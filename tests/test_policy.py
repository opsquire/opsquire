"""Tiers, auto-run, the policy seal and checkpoint export."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from support import OpsTestCase


class PolicyTest(OpsTestCase):
    def test_policy_tiers_take_the_strictest_target_and_prod_never_auto_runs(self) -> None:
        self.assertEqual(self.cli("policy", "--tier", "dev", "dev-*").returncode, 0)
        self.assertEqual(self.cli("policy", "--auto", "dev", "read", "dry").returncode, 0)
        refused = self.cli("policy", "--auto", "prod", "read")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("not one of local, dev, staging", refused.stderr)
        self.assertEqual(self.cli("policy", "--auto", "dev", "deploy").returncode, 2, "only run modes")
        shown = json.loads(self.cli("policy", "--json").stdout)
        self.assertEqual((shown["tiers"]["dev"], shown["auto_run"]["dev"]), (["dev-*"], ["read", "dry"]))

        dev = self.new("dev-read", "print(1)\n", "--target", "dev-1", area="dev-1").name
        mixed = self.new("mixed-read", "print(1)\n", "--target", "dev-1", "--target", "web1").name
        part = self.new("part-read", "print(1)\n", "--target", "dev-2/nginx", area="dev-2").name
        change = self.new("dev-change", "print(1)\n", "--changes-prod", "--rollback", "undo", "--target", "dev-3")
        for tid in (dev, mixed, part, change.name):
            self.assertEqual(self.cli("lint", tid).returncode, 0)
            self.hand_over(tid)
        seen = {tid: self.inbox_item(tid) for tid in (dev, mixed, part, change.name)}
        tiers = {tid: (it or {}).get("tier") for tid, it in seen.items()}
        self.assertEqual(tiers, {dev: "dev", mixed: "prod", part: "dev", change.name: "dev"})
        self.assertEqual([(it or {}).get("auto") for it in seen.values()], [True, False, True, True])

        self.assertEqual(self.cli("policy", "--auto", "dev", "off").returncode, 0)
        self.assertFalse((self.inbox_item(dev) or {}).get("auto"))
        self.assertIn("never", self.cli("policy").stdout)

    def test_an_unattended_apply_follows_the_auto_run_rules_and_never_touches_prod(self) -> None:
        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'ok')\n"
        tid = self.new("nightly", body, "--changes-prod", "--rollback", "undo", "--no-verify").name
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        self.assertEqual(self.cli("run", tid, unattended=True).returncode, 0, "a dry run changes nothing")
        refused = self.cli("run", tid, "--apply", unattended=True)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        rec = self.last(tid)
        self.assertEqual((rec["mode"], rec["status"], rec["origin"]), ("apply", "blocked", "unattended"))
        self.assertIn("nobody is present to start this apply: its targets (web1) are prod", rec["last_error"])
        self.assertEqual(self.cli("run", tid, "--apply").returncode, 0, "the operator at a terminal may apply")

        self.assertEqual(self.cli("policy", "--tier", "local", "web1").returncode, 0)
        self.assertEqual(self.cli("run", tid, unattended=True).returncode, 0)
        self.assertEqual(self.cli("run", tid, "--apply", unattended=True).returncode, 2, "local, but apply is not on")
        self.assertEqual(self.cli("policy", "--auto", "local", "apply").returncode, 0)
        self.assertEqual(self.cli("run", tid, "--apply", unattended=True).returncode, 0)
        self.assertEqual((self.last(tid)["status"], self.last(tid)["origin"]), ("ok", "unattended"))

        config = json.loads(self.config.read_text())
        config["policy"]["tiers"]["local"] = ["web*"]
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.cli("run", tid, unattended=True).returncode, 0)
        self.assertEqual(
            self.cli("run", tid, "--apply", unattended=True).returncode, 2, "unsealed rules count for nothing"
        )

    def marking_plan(self, areas: dict[str, str], extra: dict[str, Any] | None = None) -> tuple[Path, dict[str, Path]]:
        """A plan of one changing step per name, on its area, touching a marker only when it applies (the markers by
        task id), with box1 in the local tier, where apply auto-runs."""
        steps, markers = [], {}
        for name, area in areas.items():
            marker = self.tmp / f"applied-{name}"
            body = (
                "from pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
                f"if sq.APPLY:\n    Path({str(marker)!r}).touch()\n"
            )
            step = self.new(name, body, *self.CHANGING, area=area)
            steps.append(step)
            markers[step.name] = marker
        self.assertEqual(self.cli("policy", "--tier", "local", "box1").returncode, 0)
        self.assertEqual(self.cli("policy", "--auto", "local", "apply").returncode, 0)
        return self.make_plan(*steps, extra=extra), markers

    def test_an_unattended_plan_apply_is_refused_before_any_step_unless_all_its_targets_auto_run_apply(self) -> None:
        plan, markers = self.marking_plan({"first": "box1", "second": "web1"})
        self.assertEqual(self.cli("run", plan.name, unattended=True).returncode, 0)
        refused = self.cli("run", plan.name, "--apply", unattended=True)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        rec = self.plan_rec(plan)
        self.assertEqual((rec["mode"], rec["status"], rec["origin"]), ("apply", "blocked", "unattended"))
        self.assertIn("nobody is present to start this apply: its targets (box1, web1) are prod", rec["last_error"])
        self.assertEqual([s["status"] for s in rec["steps"]], ["skipped", "skipped"])
        for task, marker in markers.items():
            self.assertFalse(marker.exists(), f"{task} applied")
            self.assertNotIn("apply", [r["mode"] for r in json.loads(self.cli("runs", task, "--json").stdout)])

        self.assertEqual(self.cli("policy", "--tier", "local", "box1", "web1").returncode, 0)
        self.assertEqual(self.cli("run", plan.name, unattended=True).returncode, 0)
        applied = self.cli("run", plan.name, "--apply", unattended=True)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual([(s["status"], s["mode"]) for s in self.plan_rec(plan)["steps"]], [("ok", "apply")] * 2)
        self.assertTrue(all(m.exists() for m in markers.values()))

    def test_an_unattended_plan_apply_counts_the_targets_of_its_rollbacks(self) -> None:
        undo = self.new("undo", "print(1)\n", *self.CHANGING)
        plan, markers = self.marking_plan({"first": "box1"}, extra={"0": {"on_failure": undo.name}})
        self.assertEqual(self.cli("run", plan.name, unattended=True).returncode, 0)
        refused = self.cli("run", plan.name, "--apply", unattended=True)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        rec = self.plan_rec(plan)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("its targets (box1, web1) are prod", rec["last_error"])
        self.assertFalse(any(m.exists() for m in markers.values()), "the step applied before its rollback was checked")

    def test_a_plan_apply_from_a_terminal_is_not_held_to_the_unattended_rule(self) -> None:
        plan, markers = self.marking_plan({"first": "box1", "second": "web1"})
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        applied = self.cli("run", plan.name, "--apply")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(self.plan_rec(plan)["origin"], "terminal")
        self.assertTrue(all(m.exists() for m in markers.values()))

    def test_auto_run_follows_only_rules_the_operator_sealed_where_agents_cannot_write(self) -> None:
        dev = self.new("sealed-read", "print(1)\n", "--target", "dev-1", area="dev-1").name
        self.assertEqual(self.cli("lint", dev).returncode, 0)
        self.hand_over(dev)
        config = json.loads(self.config.read_text()) if self.config.is_file() else {}
        config["policy"] = {"tiers": {"dev": ["dev-*"]}, "auto_run": {"dev": ["read"]}}
        self.config.write_text(json.dumps(config))
        item = self.inbox_item(dev) or {}
        self.assertEqual((item.get("tier"), item.get("auto")), ("dev", False), "rules written by hand start nothing")
        shown = self.cli("policy")
        self.assertEqual(shown.returncode, 0)
        self.assertIn("NOT SEALED", shown.stderr)

        var = self.home / "var"
        var.mkdir(exist_ok=True)
        var.chmod(0o555)
        try:
            refused = self.cli("policy", "--auto", "dev", "read", "dry")
        finally:
            var.chmod(0o755)
        self.assertEqual(refused.returncode, 1, "a shell that cannot write var/, like an agent's, cannot seal")
        self.assertIn("NOT SEALED", refused.stderr)
        self.assertFalse((self.inbox_item(dev) or {}).get("auto"))

        self.assertEqual(self.cli("policy", "--seal").returncode, 0)
        self.assertTrue((self.inbox_item(dev) or {}).get("auto"), "sealed by the operator, the rules count")
        entries = [json.loads(x) for x in (var / "ledger/entries.jsonl").read_text().splitlines()]
        sealed = [e for e in entries if e.get("kind") == "policy"]
        self.assertEqual(sealed[-1]["data"]["auto_run"]["dev"], ["read", "dry"], "each seal is a ledger entry")

    def test_the_console_auto_runs_what_the_policy_allows_once_and_records_the_rule(self) -> None:
        self.assertEqual(self.cli("policy", "--tier", "dev", "dev-*").returncode, 0)
        self.assertEqual(self.cli("policy", "--auto", "dev", "read").returncode, 0)
        dev = self.new("auto-read", "print(1)\n", "--target", "dev-1", area="dev-1").name
        prod = self.new("prod-read", "print(1)\n", "--target", "web1").name
        for tid in (dev, prod):
            self.assertEqual(self.cli("lint", tid).returncode, 0)
            self.hand_over(tid)
        code = (
            "import json\nfrom opsquire.jobs import Jobs\nfrom opsquire.state import state\n"
            "jobs = Jobs()\nfirst = jobs.auto_run(state()['inbox'])\n"
            "codes = [j.proc.wait(timeout=100) for j in first if j.proc]\n"
            "again = jobs.auto_run(state()['inbox'])\n"
            "print(json.dumps([[j.task_id for j in first], codes, len(again)]))"
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=150)
        self.assertEqual(r.returncode, 0, r.stderr)
        started, codes, again = json.loads(r.stdout)
        self.assertEqual((started, codes, again), ([dev], [0], 0))
        rec = self.last(dev)
        self.assertEqual((rec["origin"], rec["policy"]), ("console", {"tier": "dev", "mode": "read", "by": "auto-run"}))
        self.assertFalse(list((self.home / "var/runs").glob(f"{prod}/*/run.json")), "prod waits for the operator")
        self.assertEqual(self.cli("run", prod).returncode, 0)
        self.assertIsNone(self.last(prod)["policy"], "a run the operator starts names no rule")

        meta = next(self.home.glob(f"tasks/*/{dev}/meta.json"))
        moved = json.loads(meta.read_text()) | {"targets": ["dev-1", "web1"]}
        meta.write_text(json.dumps(moved, indent=2) + "\n")
        self.env["OPSQUIRE_POLICY"] = json.dumps({"tier": "dev", "mode": "read"})
        r = self.cli("run", dev)
        del self.env["OPSQUIRE_POLICY"]
        self.assertEqual(r.returncode, 2, "a target added after the console looked takes the run out of auto-run")
        rec = self.last(dev)
        self.assertEqual((rec["status"], rec["policy"]["tier"]), ("blocked", "prod"))
        self.assertIn("started this read by policy, but its targets (dev-1, web1) are prod", rec["last_error"])

    def test_auto_run_never_starts_an_apply_while_another_apply_holds_its_targets(self) -> None:
        self.assertEqual(self.cli("policy", "--tier", "dev", "dev-*").returncode, 0)
        self.assertEqual(self.cli("policy", "--auto", "dev", "dry", "apply").returncode, 0)
        tid = self.new("auto-apply", "print(1)\n", "--target", "dev-1", *self.CHANGING, area="dev-1").name
        self.assertEqual(self.cli("run", tid).returncode, 0)
        self.write_lock("dev-1", os.getpid(), run_id="R1_apply")
        code = (
            "import json\nimport sys\nfrom pathlib import Path\nfrom opsquire.jobs import Jobs\n"
            "from opsquire.state import state\n"
            "jobs = Jobs()\nbusy = jobs.auto_run(state()['inbox'])\n"
            "Path(sys.argv[1]).write_text('{}')\n"
            "free = jobs.auto_run(state()['inbox'])\n"
            "codes = [j.proc.wait(timeout=100) for j in free if j.proc]\n"
            "print(json.dumps([len(busy), [j.mode for j in free], codes]))"
        )
        locks_file = str(self.home / "var/locks/locks.json")
        r = subprocess.run(
            [sys.executable, "-c", code, locks_file], env=self.env, capture_output=True, text=True, timeout=150
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [0, ["apply"], [0]])

    def test_each_new_checkpoint_goes_to_the_operators_command_and_audit_says_how_it_went(self) -> None:
        sink = self.tmp / "kept-elsewhere.txt"
        script = (
            "import os, sys; open(sys.argv[1], 'a').write(sys.stdin.read() + os.environ['OPSQUIRE_CHECKPOINT'] + '\\n')"
        )
        config = json.loads(self.config.read_text()) if self.config.is_file() else {}
        config["checkpoint_command"] = [sys.executable, "-c", script, str(sink)]
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(config))
        folder = self.new("exported", "print(1)\n")
        self.assertEqual(self.cli("run", folder.name).returncode, 0)
        kept = self.audit()[1]["checkpoint"]
        lines = sink.read_text().split()
        self.assertEqual(lines, [kept, kept], "stdin and the environment carry the same checkpoint")
        out = self.cli("audit").stdout
        self.assertIn(f"sent {kept}", out)

        config["checkpoint_command"] = [sys.executable, "-c", "import sys; sys.exit('remote is down')"]
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.cli("run", folder.name).returncode, 0, "a failed export never fails the run")
        code, data = self.audit()
        self.assertEqual(code, 0)
        self.assertFalse(data["export"]["ok"])
        self.assertIn("remote is down", data["export"]["error"])
        self.assertIn("NOT sent", self.cli("audit").stdout)
