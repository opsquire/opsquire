"""The tamper-evident ledger, audit, checkpoints and run snapshots."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from support import ROOT, OpsTestCase


class LedgerTest(OpsTestCase):
    def test_lock_events_go_to_the_ledger(self) -> None:
        from opsquire import locks

        dead = subprocess.Popen(["true"])
        dead.wait()
        with mock.patch.dict(os.environ, self.env), mock.patch("opsquire.ledger.append") as append:
            locks.acquire(["web1", "db1/nginx"], self.holder("A", pid=dead.pid), break_lock=False)
            locks.acquire(["db1"], self.holder("B"), break_lock=False)
            locks.acquire(["db1"], self.holder("C"), break_lock=False)
            locks.acquire(["db1"], self.holder("C"), break_lock=True)
            locks.release(["db1", "web1"], "C")
            locks.remove("web1")
        self.assertEqual({c.args[0] for c in append.call_args_list}, {"lock"})
        events = [c.args[1] for c in append.call_args_list]
        self.assertEqual(
            [(e["event"], e.get("target") or e.get("targets")) for e in events],
            [
                ("acquire", ["web1", "db1/nginx"]),
                ("reclaim", "db1/nginx"),
                ("acquire", ["db1"]),
                ("break", "db1"),
                ("acquire", ["db1"]),
                ("release", ["db1"]),
                ("remove", "web1"),
            ],
        )
        self.assertEqual((events[0]["mode"], events[0]["tokens"]), ("exclusive", {"web1": 1, "db1/nginx": 1}))
        self.assertEqual((events[1]["holder"]["run_id"], events[1]["by"]["run_id"]), ("A", "B"))
        self.assertEqual(events[1]["why"], f"pid {dead.pid} is gone")
        self.assertEqual((events[2]["tokens"], events[4]["tokens"]), ({"db1": 2}, {"db1": 3}))
        self.assertEqual((events[3]["holder"]["run_id"], events[3]["by"]["run_id"]), ("B", "C"))
        self.assertEqual([h["run_id"] for h in events[6]["holders"]], ["A"])

    def test_a_script_changing_its_own_copy_never_reaches_an_earlier_snapshot(self) -> None:
        from opsquire.model import content_digest

        folder = self.new("shared-input", "print('initial run')\n")
        config = folder / "config.json"
        config.write_text('{"value": "original"}\n')
        self.assertEqual(self.cli("run", folder.name).returncode, 0)
        old = self.last(folder.name)
        kept = Path(old["paths"]["snapshot"]) / config.relative_to(self.home)
        (folder / "main.py").write_text(
            "from pathlib import Path\n\nconfig = Path(__file__).with_name('config.json')\n"
            'config.chmod(0o600)\nconfig.write_text(\'{"value": "changed by later run"}\\n\')\n'
        )
        r = self.cli("run", folder.name)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(kept.read_text(), '{"value": "original"}\n')
        self.assertEqual(old["files"][str(config.relative_to(self.home))], content_digest(kept))
        newer = self.last(folder.name)
        own = Path(newer["paths"]["snapshot"]) / config.relative_to(self.home)
        self.assertEqual(own.read_text(), '{"value": "original"}\n', "the snapshot keeps what the run was given")
        self.assertEqual(config.read_text(), '{"value": "original"}\n')
        self.assertFalse((Path(newer["paths"]["dir"]) / ".work").exists())

    def test_runs_share_snapshot_files_and_old_logs_are_gzipped_but_still_read(self) -> None:
        folder = self.new("shared", "print('hello from the log')\n")
        for _ in range(2):
            self.assertEqual(self.cli("run", folder.name).returncode, 0)
        recs = json.loads(self.cli("runs", folder.name, "--json").stdout)
        mains = [Path(r["paths"]["snapshot"]) / "tasks" / folder.parent.name / folder.name / "main.py" for r in recs]
        self.assertEqual(mains[0].stat().st_ino, mains[1].stat().st_ino, "the same content is stored once")
        self.assertTrue(
            (self.home / "var" / "cache" / "find-index.marshal").is_file(), "a run leaves find's index warm"
        )
        self.assertFalse(mains[0].stat().st_mode & 0o222, "a shared snapshot file is read-only")
        old = Path(recs[0]["paths"]["dir"])
        run_json = old / "run.json"
        rec = json.loads(run_json.read_text())
        rec["ended"] = "2020-01-01T00:00:00.000Z"
        run_json.write_text(json.dumps(rec))
        r = self.cli("compact")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 runs", r.stdout)
        self.assertTrue((old / "output.log.gz").is_file() and not (old / "output.log").exists())
        self.assertTrue((old / "events.jsonl.gz").is_file())
        self.assertTrue((Path(recs[1]["paths"]["dir"]) / "output.log").is_file(), "a recent run keeps plain logs")
        shown = self.cli("log", recs[0]["run_id"])
        self.assertIn("hello from the log", shown.stdout, shown.stderr)
        events = json.loads(self.cli("log", recs[0]["run_id"], "--events", "--json").stdout)
        self.assertTrue(any(e.get("event") == "run_start" for e in events))
        self.assertIn("compacted 0 runs", self.cli("compact").stdout, "a compacted run is not done again")
        for r_ in recs:
            shutil.rmtree(r_["paths"]["dir"])
        self.assertIn("unused blobs removed", self.cli("compact").stdout)
        self.assertEqual([p for p in (self.home / "var" / "blobs").rglob("*") if p.is_file()], [])

    def test_ledger_chains_runs_plans_and_decisions_with_a_chain_per_actor(self) -> None:
        check = self.new("check", "print('checked')\n")
        self.assertEqual(self.cli("run", check.name).returncode, 0)
        plan = self.make_plan(check)
        self.env["OPSQUIRE_AUTHOR"] = "agent-b"
        self.assertEqual(self.cli("log", check.name).returncode, 0)
        r = self.cli("handover", check.name, "--why", "once more")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cli("hold", check.name, "--why", "after the window").returncode, 0)
        self.env["OPSQUIRE_AUTHOR"] = "test"
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        self.assertEqual(self.cli("close", check.name, "--outcome", "done", "--note", "checked").returncode, 0)

        entries = self.ledger_entries()
        self.assertEqual(
            [e["kind"] for e in entries],
            ["run_start", "run_end", "handover", "hold", "run_start", "run_start", "run_end", "run_end", "close"],
        )
        all_entries = [json.loads(x) for x in (self.home / "var/ledger/entries.jsonl").read_text().splitlines()]
        self.assertEqual([e["seq"] for e in all_entries], list(range(1, len(all_entries) + 1)))
        self.assertEqual([e["prev"] for e in all_entries], ["", *(e["hash"] for e in all_entries[:-1])])
        for author in ("test", "agent-b"):
            mine = [e for e in all_entries if e["actor"]["author"] == author]
            self.assertEqual([e["actor_prev"] for e in mine], ["", *(e["hash"] for e in mine[:-1])], author)
        plan_start, step_start = entries[4]["data"], entries[5]["data"]
        self.assertEqual((plan_start["task"], plan_start["task_kind"]), (plan.name, "plan"))
        self.assertEqual((step_start["task"], step_start["plan_run"]), (check.name, plan_start["run"]))
        self.assertEqual(entries[6]["data"]["run"], step_start["run"], "the step ends before its plan")
        self.assertEqual(entries[8]["data"]["outcome"]["state"], "done")
        self.assertEqual(entries[2]["actor"]["author"], "agent-b")

        code, out = self.audit()
        self.assertEqual(code, 0, out["problems"])
        self.assertEqual(out["runs"], {"sealed": 3, "before_ledger": 0, "running": 0, "unsealed": 0})
        self.assertEqual(sorted(k.rsplit(" ", 1)[1] for k in out["actors"]), ["(agent-b)", "(test)"])
        self.assertEqual(out["checkpoints"], 3)
        self.assertRegex(out["checkpoint"], rf"^{len(all_entries)}:[0-9a-f]{{64}}$")
        text = self.cli("audit")
        self.assertEqual(text.returncode, 0)
        self.assertIn(f"checkpoint {out['checkpoint']}\n", text.stdout)

    def test_audit_names_every_change_to_a_sealed_run_or_the_ledger(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import store

        folder = self.new("sealed", "print('hello from the log')\n")
        for _ in range(2):
            self.assertEqual(self.cli("run", folder.name).returncode, 0)
        old = Path(json.loads(self.cli("runs", folder.name, "--json").stdout)[0]["paths"]["dir"])
        label = f"run {folder.name}/{old.name}"
        entries_file = self.home / "var/ledger/entries.jsonl"
        main = old / "snapshot" / folder.relative_to(self.home) / "main.py"
        self.assertEqual(self.audit()[0], 0)

        def caught(paths: list[Path], change: Any, *expected: str) -> None:
            saved = {p: p.read_bytes() for p in paths}
            change()
            code, out = self.audit()
            for p, data in saved.items():
                p.unlink(missing_ok=True)
                p.write_bytes(data)
            self.assertEqual(code, 1, expected)
            for text in expected:
                self.assertTrue(any(text in p for p in out["problems"]), (text, out["problems"]))
            self.assertEqual(self.audit()[0], 0, "restored")

        def edit_json(path: Path, key: str, value: Any) -> None:
            rec = json.loads(path.read_text())
            rec[key] = value
            path.write_text(json.dumps(rec, indent=2) + "\n")

        def entry_lines(change: Any) -> Any:
            return lambda: entries_file.write_text("".join(change(entries_file.read_text().splitlines(True))))

        caught(
            [old / "run.json"], lambda: edit_json(old / "run.json", "status", "failed"), f"{label}: run.json changed"
        )
        caught(
            [old / "output.log"],
            lambda: (old / "output.log").write_text((old / "output.log").read_text().replace("hello", "bye")),
            f"{label}: output.log changed",
        )
        store.gzip_file(old / "events.jsonl")
        self.assertEqual(self.audit()[0], 0, "a gzipped log is read by its content")
        packed = old / "events.jsonl.gz"
        caught(
            [packed],
            lambda: packed.write_bytes(gzip.compress(gzip.decompress(packed.read_bytes()).replace(b"ok", b"no"))),
            f"{label}: events.jsonl changed",
        )

        def swap_snapshot() -> None:
            main.unlink()
            main.write_text("print('not what ran')\n")

        caught([main], swap_snapshot, f"{label}: snapshot file {main.relative_to(old / 'snapshot')} changed")
        caught([main], main.unlink, f"{label}: snapshot file {main.relative_to(old / 'snapshot')} is missing")
        caught([entries_file], entry_lines(lambda x: [x[0], *x[2:]]), "entry 2: numbered 3 after 1", "entry 2: does")
        caught([entries_file], entry_lines(lambda x: [x[1], x[0], *x[2:]]), "entries were removed or reordered")
        caught([entries_file], entry_lines(lambda x: x[:-1]), "entries but the ledger has")
        caught(
            [entries_file],
            entry_lines(lambda x: [x[0].replace('"read"', '"apply"'), *x[1:]]),
            "entry 1: changed after it was written",
            "entry 2: does not follow",
        )
        moved = self.tmp / "moved-away"

        def remove_run() -> None:
            old.rename(moved)

        try:
            remove_run()
            code, out = self.audit()
        finally:
            moved.rename(old)
        self.assertEqual(code, 1)
        self.assertIn(f"{label}: in the ledger but its record is gone", out["problems"])

    def test_audit_while_another_run_appends_finds_nothing_wrong(self) -> None:
        from opsquire import ledger

        folder = self.new("busy", "print('healthy')\n")
        self.assertEqual(self.cli("run", folder.name).returncode, 0)
        read = ledger.Ledger.read
        ran: list[int] = []

        def read_then_a_run() -> Any:
            snapshot = read()
            if not ran:
                ran.append(self.cli("run", folder.name).returncode)
            return snapshot

        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(ledger.Ledger, "read", side_effect=read_then_a_run):
                during = ledger.audit()
            self.assertEqual(ran, [0])
            self.assertEqual(during.problems, [])
            self.assertEqual(during.runs["running"], 1, "the run that began during the audit")
            self.assertTrue(ledger.audit().ok)

    def test_audit_against_a_kept_checkpoint_catches_a_rewritten_history(self) -> None:
        folder = self.new("history", "print('the original')\n")
        self.assertEqual(self.cli("run", folder.name).returncode, 0)
        kept = self.audit()[1]["checkpoint"]
        self.assertEqual(self.cli("run", folder.name).returncode, 0)
        code, out = self.audit("--against", kept)
        self.assertEqual((code, out["against"]["consistent"]), (0, True), out["problems"])
        self.assertTrue(out["against"]["proof"])
        self.assertIn(f"extends {kept}", self.cli("audit", "--against", kept).stdout)

        runs = [Path(r["paths"]["dir"]) for r in json.loads(self.cli("runs", folder.name, "--json").stdout)]
        (runs[0] / "output.log").write_text((runs[0] / "output.log").read_text().replace("original", "forgery"))
        shutil.rmtree(self.home / "var/ledger")
        self.reseal(*runs)
        code, out = self.audit()
        self.assertEqual(code, 0, "a history rewritten whole passes on its own")
        code, out = self.audit("--against", kept)
        self.assertEqual((code, out["against"]["consistent"]), (1, False))
        self.assertIn(f"--against {kept}: the ledger does not extend that checkpoint", out["problems"][0])
        size, root = kept.split(":")
        code, out = self.audit("--against", f"{int(size) + 50}:{root}")
        self.assertIn("history was cut", out["problems"][0])
        r = self.cli("audit", "--against", "12:not-a-root")
        self.assertEqual(r.returncode, 2)
        self.assertIn("is not a checkpoint", r.stderr)

    def test_audit_run_proves_its_entries_are_in_the_tree(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import ledger

        folder = self.new("proved", "print(1)\n")
        for _ in range(3):
            self.assertEqual(self.cli("run", folder.name).returncode, 0)
        run_id = self.last(folder.name)["run_id"]
        r = self.cli("audit", "--run", run_id, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        found = [x for x in report["entries"] if x["entry"]["kind"] != "lock"]
        self.assertEqual([x["entry"]["kind"] for x in found], ["run_start", "run_end"])
        held = [x["entry"]["data"]["event"] for x in report["entries"] if x["entry"]["kind"] == "lock"]
        self.assertEqual(held, ["acquire", "release"], "its lock is the run's too")
        size, root = ledger.parse_checkpoint(report["checkpoint"])
        for x in found:
            proof = [bytes.fromhex(p) for p in x["proof"]]
            here = ledger.leaf(x["entry"]["hash"])
            self.assertTrue(x["included"])
            self.assertTrue(ledger.verify_inclusion(x["index"], size, here, proof, root))
            self.assertFalse(ledger.verify_inclusion(x["index"], size, here, proof[::-1], root))
        self.assertIn("run_start by ", self.cli("audit", "--run", run_id).stdout)
        self.assertEqual(self.cli("audit", "--run", "20200101T000000000Z_read").returncode, 1)

    def test_ledger_appends_from_many_processes_are_whole_and_in_one_chain(self) -> None:
        script = "from opsquire import ledger\nfor i in range(40):\n    assert ledger.append('test', {'i': i})\n"
        writers = [
            subprocess.Popen([sys.executable, "-c", script], env={**self.env, "OPSQUIRE_AUTHOR": f"writer-{n}"})
            for n in range(6)
        ]
        for w in writers:
            self.assertEqual(w.wait(timeout=120), 0)
        raw = (self.home / "var/ledger/entries.jsonl").read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        entries = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual([e["seq"] for e in entries], list(range(1, 241)))
        for n in range(6):
            mine = [e["data"]["i"] for e in entries if e["actor"]["author"] == f"writer-{n}"]
            self.assertEqual(mine, list(range(40)))
        code, out = self.audit()
        self.assertEqual(code, 0, out["problems"])
        self.assertEqual(sorted(out["actors"].values()), [40] * 6)

    def test_an_unwritable_ledger_records_nothing_and_fails_nothing(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root writes through read-only modes")
        folder = self.new("quiet", "print(1)\n")
        ledger_dir = self.home / "var/ledger"
        ledger_dir.mkdir(parents=True)
        probe = "from opsquire import ledger\nprint(ledger.append('test', {}), ledger.checkpoint())\n"

        def unwritable() -> None:
            for p in [*ledger_dir.iterdir(), ledger_dir]:
                p.chmod(0o555)
            try:
                r = self.cli("run", folder.name)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                r = subprocess.run([sys.executable, "-c", probe], env=self.env, capture_output=True, text=True)
                self.assertEqual(r.stdout, "None None\n", r.stderr)
                self.assertEqual(self.cli("hold", folder.name, "--why", "later").returncode, 0)
            finally:
                for p in [ledger_dir, *ledger_dir.iterdir()]:
                    p.chmod(0o755)

        unwritable()
        self.assertEqual(list(ledger_dir.iterdir()), [])
        code, out = self.audit()
        self.assertEqual((code, out["runs"]["before_ledger"]), (0, 1), out["problems"])
        self.assertEqual(self.cli("run", folder.name).returncode, 0)
        unwritable()
        code, out = self.audit()
        self.assertEqual(out["runs"], {"sealed": 1, "before_ledger": 1, "running": 0, "unsealed": 0})
        missing = self.last(folder.name)["run_id"]
        self.assertEqual(
            (code, out["problems"]),
            (1, [f"run {folder.name}/{missing}: started after the ledger began but is not in it"]),
        )

    def test_decisions_an_agent_writes_from_its_sandbox_are_observed_once(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root writes through read-only modes")
        closed = self.new("closed-by-agent", "print(1)\n")
        handed = self.new("handed-by-agent", "print(1)\n")
        self.assertEqual(self.cli("run", closed.name).returncode, 0)
        agent = "claude-code 2d39fd02"
        var = self.home / "var"
        modes = {p: p.stat().st_mode for p in [var, *var.rglob("*")]}
        try:
            for p in modes:
                p.chmod(0o555)
            self.env["OPSQUIRE_AUTHOR"] = agent
            r = self.cli("close", closed.name, "--outcome", "done", "--note", "it landed")
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self.cli("handover", handed.name, "--why", "needs a run")
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            for p, mode in modes.items():
                p.chmod(mode)
        self.env["OPSQUIRE_AUTHOR"] = "test"
        self.assertEqual([e["kind"] for e in self.ledger_entries()], ["run_start", "run_end"])
        code, out = self.audit()
        self.assertEqual((code, out["unrecorded"]), (0, [f"{closed.name}: outcome", f"{handed.name}: handover"]))
        self.assertIn("2 decisions in meta.json were never recorded", self.cli("audit").stdout)

        self.assertEqual(self.cli("hold", closed.name, "--why", "after the window").returncode, 0)
        for _ in range(2):
            self.assertEqual(self.cli("run", handed.name).returncode, 0)
        observed = [e for e in self.ledger_entries() if e["kind"] == "observed"]
        self.assertEqual(
            sorted((e["data"]["task"], e["data"]["field"], e["data"]["by"]) for e in observed),
            [(closed.name, "outcome", agent), (handed.name, "handover", agent)],
        )
        self.assertEqual({e["actor"]["author"] for e in observed}, {"test"})
        outcome = next(e["data"]["value"] for e in observed if e["data"]["field"] == "outcome")
        self.assertEqual((outcome["state"], outcome["note"]), ("done", "it landed"))
        code, out = self.audit()
        self.assertEqual((code, out["unrecorded"]), (0, []), out["problems"])

    def test_runs_before_the_ledger_and_a_torn_line_are_told_apart(self) -> None:
        folder = self.new("early", "print(1)\n")
        self.assertEqual(self.cli("run", folder.name).returncode, 0)
        shutil.rmtree(self.home / "var/ledger")
        self.assertEqual(self.cli("run", folder.name).returncode, 0)
        code, out = self.audit()
        self.assertEqual(code, 0, out["problems"])
        self.assertEqual((out["runs"]["before_ledger"], out["runs"]["sealed"]), (1, 1))
        self.assertIn("1 sealed, 1 before the ledger", self.cli("audit").stdout)

        entries_file = self.home / "var/ledger/entries.jsonl"
        torn = len(entries_file.read_text().splitlines()) + 1
        with entries_file.open("ab") as f:
            f.write(b'{"seq": 9, "ha')
        self.assertEqual(self.cli("hold", folder.name, "--why", "later").returncode, 0)
        lines = entries_file.read_text().splitlines()
        self.assertEqual((len(lines), json.loads(lines[-1])["seq"]), (torn + 1, torn + 1))
        code, out = self.audit()
        self.assertEqual((code, out["problems"]), (1, [f"entry {torn}: not a ledger entry"]))


class LedgerTree(unittest.TestCase):
    """The ledger's Merkle tree against the worked example of RFC 6962 (seven leaves), computed by hand."""

    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))

    def test_tree_hash_and_proofs_match_the_rfc_example(self) -> None:
        import hashlib

        from opsquire import ledger

        def node(left: bytes, right: bytes) -> bytes:
            return hashlib.sha256(b"\x01" + left + right).digest()

        entries = [hashlib.sha256(bytes([n])).hexdigest() for n in range(7)]
        a, b, c, d, e, f, j = (hashlib.sha256(b"\x00" + bytes.fromhex(h)).digest() for h in entries)
        g, h, i = node(a, b), node(c, d), node(e, f)
        k, l_ = node(g, h), node(i, j)
        root = node(k, l_)
        leaves = [ledger.leaf(x) for x in entries]
        self.assertEqual(leaves, [a, b, c, d, e, f, j])
        self.assertEqual(ledger.tree_hash(leaves), root)
        self.assertEqual(ledger.tree_hash([]), hashlib.sha256(b"").digest())
        roots = {1: a, 2: g, 3: node(g, c), 4: k, 6: node(k, i)}
        for size, expected in roots.items():
            self.assertEqual(ledger.tree_hash(leaves[:size]), expected, size)
        paths = {0: [b, h, l_], 3: [c, g, l_], 4: [f, j, k], 6: [i, k]}
        for index, path in paths.items():
            self.assertEqual(ledger.inclusion_proof(index, leaves), path, index)
            self.assertTrue(ledger.verify_inclusion(index, 7, leaves[index], path, root))
            self.assertFalse(ledger.verify_inclusion(index, 7, leaves[index], path, node(root, root)))
            self.assertFalse(ledger.verify_inclusion(index ^ 1, 7, leaves[index], path, root))
        proofs = {3: [c, d, g, l_], 4: [l_], 6: [i, j, k]}
        for size, proof in proofs.items():
            self.assertEqual(ledger.consistency_proof(size, leaves), proof, size)
            self.assertTrue(ledger.verify_consistency(size, 7, roots[size], root, proof))
            self.assertFalse(ledger.verify_consistency(size, 7, node(a, a), root, proof))
            self.assertFalse(ledger.verify_consistency(size, 7, roots[size], root, proof[:-1]))

    def test_every_proof_of_a_small_tree_verifies_and_the_frontier_gives_the_same_root(self) -> None:
        import hashlib

        from opsquire import ledger

        hashes = [hashlib.sha256(str(n).encode()).hexdigest() for n in range(17)]
        for n in range(1, 18):
            leaves = [ledger.leaf(x) for x in hashes[:n]]
            root = ledger.tree_hash(leaves)
            tip = ledger.Tip()
            for x in hashes[:n]:
                tip.add(None, x)
            self.assertEqual(tip.root(), root, n)
            for index in range(n):
                proof = ledger.inclusion_proof(index, leaves)
                self.assertTrue(ledger.verify_inclusion(index, n, leaves[index], proof, root), (n, index))
                if proof:
                    bent = [proof[0][::-1], *proof[1:]]
                    self.assertFalse(ledger.verify_inclusion(index, n, leaves[index], bent, root), (n, index))
            for m in range(n + 1):
                proof = ledger.consistency_proof(m, leaves)
                old = ledger.tree_hash(leaves[:m])
                self.assertTrue(ledger.verify_consistency(m, n, old, root, proof), (m, n))
                if 0 < m < n:
                    self.assertFalse(ledger.verify_consistency(m, n, old[::-1], root, proof), (m, n))
                    self.assertFalse(ledger.verify_consistency(m, n, old, root[::-1], proof), (m, n))
