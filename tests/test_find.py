"""find, the catalog and reuse of general scripts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

from support import CLI, OpsTestCase


class FindTest(OpsTestCase):
    def test_find_splits_general_scripts_from_past_tasks_newest_on_ties_with_the_track_record(self) -> None:
        failing = 'import opsquire_helpers as sq\n\nsq.error("vendor refused the login")\nraise SystemExit(1)\n'
        self.new("alpha-rotate", "print(1)\n")
        self.new("gamma-rotate", "print(1)\n")
        self.new("beta-rotate", failing, "--uses", "web1_rotate-keys")
        r = self.cli("new", "web1", "rotate-keys", "--general", "--purpose", "rotate keys on web hosts")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cli("run", "beta-rotate").returncode, 1)
        found = json.loads(self.cli("find", "rotate", "--json").stdout)
        self.assertEqual([h["id"] for h in found["reuse"]], ["web1_rotate-keys"])
        names = [h["id"].split("_", 1)[1] for h in found["history"]]
        self.assertNotIn("web1_rotate-keys", names, "a general script is only under reuse")
        self.assertLess(names.index("web1_gamma-rotate"), names.index("web1_alpha-rotate"), "newest wins a tie")
        beta = next(h for h in found["history"] if h["id"].endswith("beta-rotate"))
        self.assertEqual(
            (beta["uses"], beta["track"]["runs"], beta["track"]["ok_rate"]), (["web1_rotate-keys"], 1, 0.0)
        )
        self.assertIn("vendor refused the login", beta["last_failure"])
        general = found["reuse"][0]
        self.assertEqual((general["track"]["runs"], general["users"]), (1, 1), "its users' runs count")
        self.assertEqual(general["voters"], [beta["id"]])
        self.assertIn("vendor refused the login", general["last_failure"]["last_error"])
        self.assertIsNone(found["hint"])
        text = self.cli("find", "rotate").stdout
        self.assertTrue(text.startswith("Scripts to reuse\nSCORE  STATUS"), text)
        self.assertIn("\nPast tasks like this\nSCORE  STATUS", text)
        self.assertRegex(text, r"web1_beta-rotate +-> web1_rotate-keys; failed: vendor refused the login")

    def test_find_reads_design_docstrings_runs_threads_word_forms_and_synonyms(self) -> None:
        designed = self.new("designed", "print(1)\n")
        (designed / "design.md").write_text("# designed\n\nThe zeppelin hangar needs a second door.\n")
        body = '"""Rotate the flux capacitor."""\n\nimport argparse\n\nargparse.ArgumentParser(description="tune it")\n'
        self.new("documented", body)
        self.new(
            "crashing",
            'import opsquire_helpers as sq\n\nsq.error("quasar overflow in the feed")\nraise SystemExit(1)\n',
        )
        self.assertEqual(self.cli("run", "crashing").returncode, 1)
        self.new("threaded", "print(1)\n", "--thread", "night-outage")
        (self.home / "threads").mkdir(exist_ok=True)
        (self.home / "threads/night-outage.md").write_text("The wombat cable came loose.\n")
        r = self.cli("new", "web1", "count-rejects", "--purpose", "count the login rejects of the day")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.new("free-disk", "print(1)\n", "--target", "db9")
        (self.home / "synonyms.json").write_text(json.dumps({"groups": [["frobnicate", "gizmo"]]}))
        self.new("frobnicate-cache", "print(1)\n")

        def top(*words: str) -> str:
            hits = json.loads(self.cli("find", *words, "--json").stdout)["history"]
            return str(hits[0]["id"].split("_", 1)[1]) if hits else ""

        self.assertEqual(top("zeppelin"), "web1_designed")
        self.assertEqual(top("capacitor"), "web1_documented")
        self.assertEqual(top("tune"), "web1_documented", "argparse description")
        self.assertEqual(top("quasar", "overflow"), "web1_crashing", "the last run's error")
        self.assertEqual(top("wombat"), "web1_threaded", "the thread's notes")
        self.assertEqual(top("rejections"), "web1_count-rejects", "word forms meet")
        self.assertEqual(top("storage", "space"), "web1_free-disk", "packaged synonyms")
        self.assertEqual(top("gizmo"), "web1_frobnicate-cache", "the home's own synonyms")
        self.assertEqual(top("hangar", "second", "door", "plan"), "web1_designed", "template words say nothing")

    def test_find_keeps_hyphenated_words_whole_and_split(self) -> None:
        r = self.cli("new", "web1", "joined", "--purpose", "read it with the web-logs tool")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("new", "web1", "apart", "--purpose", "web pages and their logs")
        self.assertEqual(r.returncode, 0, r.stderr)
        hits = json.loads(self.cli("find", "web_logs", "--json").stdout)["history"]
        self.assertEqual([h["id"].split("_", 2)[2] for h in hits], ["joined", "apart"])
        self.assertGreater(hits[0]["score"], 2 * hits[1]["score"])

    def test_new_names_general_scripts_to_reuse_without_blocking(self) -> None:
        r = self.cli("new", "web", "deploy-config", "--general", "--purpose", "deploy the web config to web hosts")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("maybe reuse", r.stderr)
        r = self.cli("new", "web2", "deploy-config", "--purpose", "deploy the web config to web2", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("opsquire: maybe reuse general/web_deploy-config: deploy the web config to web hosts", r.stderr)
        self.assertEqual([c["id"] for c in json.loads(r.stdout)["reuse_candidates"]], ["web_deploy-config"])
        r = self.cli(
            "new", "web3", "deploy-config", "--purpose", "deploy the web config", "--uses", "web_deploy-config"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("maybe reuse", r.stderr)
        r = self.cli("new", "web4", "count-users", "--purpose", "count the users of web4", "--json")
        self.assertEqual(json.loads(r.stdout)["reuse_candidates"], [])

    def test_catalog_keeps_general_scripts_recent_and_open_tasks(self) -> None:
        self.cli("new", "web1", "helper", "--general", "--purpose", "shared helper")
        old_done = self.new("old-done", "print(1)\n")
        old_draft = self.new("old-draft", "print(1)\n")
        self.new("fresh-done", "print(1)\n")
        for name in ("old-done", "fresh-done"):
            self.assertEqual(self.cli("run", name).returncode, 0)
        for folder in (old_done, old_draft):
            meta = json.loads((folder / "meta.json").read_text())
            meta["created"] = "2020-01-01T00:00:00.000Z"
            (folder / "meta.json").write_text(json.dumps(meta))
        self.assertEqual(self.cli("docs").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertIn("**web1_helper**: shared helper", catalog)
        self.assertIn("web1_fresh-done [done", catalog)
        self.assertIn("web1_old-draft [", catalog, "an open task stays whatever its age")
        self.assertNotIn("old-done", catalog)
        self.assertIn("1 older settled tasks are left out: `opsquire find <words>`", catalog)

    def test_find_index_cache_gives_the_uncached_ranking_and_survives_a_read_only_cache(self) -> None:
        designed = self.new("rotate-keys", "print(1)\n")
        (designed / "design.md").write_text("# rotate\n\nRotate the vendor keys after the outage.\n")
        self.new("rotate-certs", '"""Rotate the vendor certificates."""\n', "--thread", "vendor-outage")
        r = self.cli("new", "web1", "rotate-all", "--general", "--purpose", "rotate keys and certificates")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.new("count-rejects", "print(1)\n", "--uses", "web1_rotate-all")
        self.assertEqual(self.cli("run", "count-rejects").returncode, 0)
        cache = self.tmp / "cache"
        blocked = self.tmp / "not-a-dir"
        blocked.write_text("")
        queries = (["rotate", "vendor"], ["rejections"], ["vendor", "outage"], ["rotate-keys"])

        def found(cache_dir: Path, words: list[str]) -> Any:
            env = {**self.env, "OPSQUIRE_CACHE_DIR": str(cache_dir)}
            r = subprocess.run([*CLI, "find", *words, "--json"], env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)

        for words in queries:
            uncached = found(blocked / "cache", words)
            self.assertTrue(uncached, words)
            self.assertEqual(found(cache, words), uncached, "cold cache")
            self.assertEqual(found(cache, words), uncached, "warm cache")
        index = cache / "find-index.marshal"
        self.assertTrue(index.is_file())
        cache.chmod(0o555)
        self.addCleanup(cache.chmod, 0o755)
        before = index.stat().st_mtime_ns
        (designed / "design.md").write_text("# rotate\n\nThe zeppelin hangar.\n")
        self.assertEqual(
            [h["id"] for h in found(cache, ["zeppelin"])["history"]], [designed.name], "stale entry rebuilt in memory"
        )
        self.assertEqual(index.stat().st_mtime_ns, before)
        self.assertEqual(list(cache.iterdir()), [index], "no temp file left in a read-only cache")

    def test_find_votes_lift_the_general_script_matching_tasks_use_and_damp_a_broad_helper(self) -> None:
        self.eval_home()
        found = json.loads(self.cli("find", "zeppelin", "--json").stdout)
        reuse = {h["id"]: h for h in found["reuse"]}
        self.assertEqual(found["reuse"][0]["id"], "web_rotate-keys", "it says nothing of zeppelins, its users do")
        self.assertEqual((reuse["web_rotate-keys"]["own"], reuse["web_rotate-keys"]["users"]), (0.0, 2))
        self.assertEqual(
            reuse["web_rotate-keys"]["voters"],
            ["20261101-1000_web1_rotate-zeppelin-keys", "20261102-1000_web2_zeppelin-leak"],
        )
        helper = reuse["web_ssh"]
        self.assertGreater(helper["votes"], reuse["web_rotate-keys"]["votes"], "more matching users vote for it")
        self.assertLess(helper["score"], reuse["web_rotate-keys"]["score"], "but a broad helper's votes are damped")
        self.assertEqual({h["id"] for h in found["history"]} & set(reuse), set(), "the sections do not overlap")
        self.assertEqual(found["history"][0]["kind"], "task")

    def test_find_shows_how_to_call_a_general_script(self) -> None:
        cli = (
            "import argparse\n\nap = argparse.ArgumentParser()\nap.add_argument('--host', required=True)\n"
            "ap.add_argument('--dry', action='store_true')\nap.add_argument('--mode', choices=['plan', 'apply'])\n"
            "ap.add_argument('--from', dest='sources', metavar='LIST')\nap.add_argument('targets', nargs='+')\n"
        )
        self.fixture("web_rotate-keys", "rotate the vendor api keys", body=cli)
        said = '"""Purge the cdn.\n\nUsage (from a task): main.py --site S {soft|hard}\n"""\n'
        self.fixture("web_purge-cdn", "purge the vendor cdn cache", body=said)
        self.fixture("web_vendor-lib", "library for the vendor api, not run directly")
        (self.home / "docs").mkdir()
        (self.home / "docs/web_vendor-lib.md").write_text("# web_vendor-lib\n")
        user = "20261101-1000_web1_rotate-vendor-keys"
        self.fixture(user, "rotate the vendor keys on web1", "web_rotate-keys")
        other = "20261102-1000_web2_rotate-vendor-keys"
        self.fixture(other, "rotate the vendor keys on web2", "web_rotate-keys")
        self.fixture_run(user, "2026-11-01T10:00:00.000Z", "ok", 30.0, "--host", "web1")
        self.fixture_run(other, "2026-11-02T10:00:00.000Z", "ok", 90.0, "--host", "web2", "--dry")
        self.fixture_run(user, "2026-11-03T10:00:00.000Z", "error", 5.0, error="vendor refused the login")
        found = json.loads(self.cli("find", "vendor", "--json").stdout)
        hits = {h["id"]: h for h in found["reuse"]}
        keys = hits["web_rotate-keys"]
        self.assertEqual(keys["usage"], "--host HOST [--dry] [--mode {plan|apply}] [--from LIST] targets ...")
        self.assertEqual(
            {k: keys["example"][k] for k in ("task", "mode", "argv")},
            {"task": other, "mode": "read", "argv": ["--host", "web2", "--dry"]},
        )
        self.assertEqual((keys["typical_s"], keys["users"]), (60.0, 2))
        self.assertEqual(
            (keys["last_failure"]["task"], keys["last_failure"]["last_error"]), (user, "vendor refused the login")
        )
        self.assertEqual(hits["web_purge-cdn"]["usage"], "main.py --site S {soft|hard}")
        lib = hits["web_vendor-lib"]
        self.assertEqual(
            (lib["usage"], lib["doc"], lib["example"]), ("", str(self.home / "docs/web_vendor-lib.md"), None)
        )
        text = self.cli("find", "vendor").stdout
        line = next(x for x in text.splitlines() if "args: --host HOST" in x)
        self.assertIn(f"; e.g. {other} (read ok 2026-11-02 10:00 UTC with --host web2 --dry); typically ~60 s", line)
        self.assertIn("; last failed 2026-11-03 10:00 UTC: vendor refused the login", line)
        self.assertIn("API: docs/web_vendor-lib.md", text)

    def test_find_hints_at_a_general_script_only_for_one_job_done_again(self) -> None:
        self.fixture("db_vacuum", "vacuum a postgres database")
        for i, area in enumerate(("web1", "web2", "db3"), 1):
            day = f"2026-11-0{i}T10:00:00.000Z"
            self.fixture(f"2026110{i}-1000_{area}_purge-stale-cache", f"purge the stale cache on {area}", created=day)
        found = json.loads(self.cli("find", "purge", "stale", "cache", "--json").stdout)
        self.assertEqual(found["reuse"], [])
        hint = found["hint"]
        self.assertEqual(hint["shape"], ["cache", "purge"])
        self.assertEqual(len(hint["tasks"]), 3)
        self.assertIn(
            "hint: no general script fits, but 3 past tasks made on different days share 'cache purge'",
            self.cli("find", "purge", "stale", "cache").stdout,
        )
        self.fixture("web_purge-cache", "purge the stale cache of a web host")
        self.assertIsNone(json.loads(self.cli("find", "purge", "stale", "cache", "--json").stdout)["hint"], "one fits")
        shutil.rmtree(self.home / "general/web_purge-cache")
        for folder in (self.home / "tasks/2026-11").iterdir():
            meta = json.loads((folder / "meta.json").read_text())
            (folder / "meta.json").write_text(json.dumps(meta | {"created": "2026-11-05T10:00:00.000Z"}))
        self.assertIsNone(json.loads(self.cli("find", "purge", "stale", "cache", "--json").stdout)["hint"], "one day")

    def test_find_eval_scores_the_reuse_ranking_on_the_homes_own_tasks(self) -> None:
        self.eval_home()
        r = self.cli("find", "--eval", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        result = json.loads(r.stdout)
        self.assertEqual((result["cases"], result["helpers_left_out"]), (9, ["web_ssh"]))
        ranks = {c["task"].split("_", 1)[1]: c["rank"] for c in result["cases_detail"]}
        first = ["web1_rotate-zeppelin-keys", "web2_zeppelin-leak", "db1_vacuum-invoices", "db2_ledger-bloat"]
        first += ["db5_invoices-upkeep", "shop_purge-cache", "shop_release-cache"]
        self.assertEqual(ranks, dict.fromkeys(first, 1) | {"web4_cdn-keys": 2, "db8_fsck-volume": None})
        self.assertEqual(result["recall"], {"@1": 0.78, "@3": 0.89, "@5": 0.89, "@10": 0.89})
        self.assertEqual(result["mrr"], 0.833)
        self.assertEqual([(w["general"], w["missed"], w["cases"]) for w in result["worst"]], [("db_vacuum", 1, 4)])
        text = self.cli("find", "--eval").stdout
        self.assertIn("9 cases: tasks that use a general script (broad helpers left out: web_ssh)", text)
        self.assertIn("recall@1 0.78  recall@3 0.89  recall@5 0.89  recall@10 0.89  MRR 0.833", text)
        self.assertIn("db_vacuum: 1 of 4, e.g. 20261109-1000_db8_fsck-volume (not found): fsck the ext4", text)
        r = self.cli("find", "--eval", "zeppelin")
        self.assertEqual((r.returncode, r.stderr.count("takes no words")), (2, 1))
        r = self.cli("find")
        self.assertEqual((r.returncode, r.stderr.count("find needs words")), (2, 1))

    def test_find_index_rebuilds_only_what_changed(self) -> None:
        from opsquire.query import SearchIndex

        first = self.new("first", "print(1)\n")
        self.new("second", "print(1)\n")
        env = {"OPSQUIRE_HOME": str(self.home), "OPSQUIRE_CACHE_DIR": str(self.tmp / "cache")}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(len(SearchIndex().rebuilt), 2)
            self.assertEqual(SearchIndex().rebuilt, [])
            (first / "design.md").write_text("# first\n\nThe zeppelin hangar.\n")
            again = SearchIndex()
            self.assertEqual(again.rebuilt, [str(first)])
            self.assertIn("zeppelin", again.entries[str(first)]["words"])
            third = self.new("third", "print(1)\n")
            self.assertEqual(SearchIndex().rebuilt, [str(third)])
        hits = json.loads(self.cli("find", "zeppelin", "--json").stdout)["history"]
        self.assertEqual([h["id"] for h in hits], [first.name])

    def test_find_ranks_matches_and_skips_unrelated(self) -> None:
        self.new("restart-workers", "print(1)\n", "--target", "web1")
        self.new("cost-report", "print(1)\n", area="reports")
        hits = json.loads(self.cli("find", "restart", "web1", "--json").stdout)["history"]
        self.assertEqual([h["id"].split("_", 1)[1] for h in hits], ["web1_restart-workers"])
        self.assertEqual(hits[0]["matched"], ["restart", "web1"])
        nothing: dict[str, Any] = {"reuse": [], "history": [], "hint": None}
        self.assertEqual(json.loads(self.cli("find", "nothing-like-this", "--json").stdout), nothing)
        self.assertEqual(self.cli("find", "nothing-like-this").stdout.count("(none)"), 2)

    def test_docs_catalog_and_api_pages_follow_the_code(self) -> None:
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        code = '"""Doubling helpers."""\n\n\ndef double(x: int) -> int:\n    """Twice x."""\n    return x * 2\n'
        (g / "logic.py").write_text(code)
        (g / "test_logic.py").write_text(self.PASSING_TEST)
        self.new("user", "print(1)\n", "--uses", "web1_shared")
        self.assertEqual(self.cli("lint", "web1_shared").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertIn("**web1_shared**: shared logic (lint-ok; tests: 2; used by 1; never run)", catalog)
        self.assertRegex(catalog, r"\d{8}-\d{4}_web1_user \[draft, task; never run\]: test user \(uses web1_shared\)")
        self.assertEqual(self.cli("run", "web1_user").returncode, 0)
        self.assertEqual(self.cli("run", "web1_user").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertIn("used by 1; runs 2, 2 ok read)", catalog)
        self.assertRegex(catalog, r"web1_user \[done, task; runs 2, 2 ok read\]")
        page = (self.home / "docs/web1_shared.md").read_text()
        self.assertIn("- runs (it and its users): runs 2, 2 ok read, last 20", page)
        page = (self.home / "docs/web1_shared.md").read_text()
        self.assertIn("`double(x: int) -> int`: Twice x.", page)
        self.assertIn("T: double", page)
        self.assertIn("Doubling helpers.", page)
        shutil.rmtree(g)
        self.assertEqual(self.cli("docs").returncode, 0)
        self.assertFalse((self.home / "docs/web1_shared.md").exists())

    def test_catalog_and_suggestions_count_script_versions(self) -> None:
        folder = self.new("replay", "print(1)\n")
        (folder / "design.md").write_text("# replay\n")
        for n in range(3):
            (folder / "main.py").write_text(f"print({n})\n")
            self.assertEqual(self.cli("lint", "replay").returncode, 0)
            self.assertEqual(self.cli("run", "replay").returncode, 0)
        (folder / "design.md").write_text("# replay\n\nnotes\n")
        self.assertEqual(self.cli("lint", "replay").returncode, 0)
        self.assertEqual(self.cli("run", "replay").returncode, 0)
        self.assertEqual(self.cli("docs").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertIn("runs 4, 4 ok read, 3 script versions", catalog, "design notes are not a version")
        view = json.loads(self.cli("threads", "--json").stdout)["tasks"][folder.name]
        self.assertEqual(view["stats"]["versions"], 3)
        self.assertIn("4 runs over 3 script versions", " ".join(s["text"] for s in view["suggestions"]))
        lines = self.cli("show", "replay").stdout.splitlines()
        at = next(line for line in lines if line.startswith("STATUS")).index("SCRIPT")
        scripts = [line[at:].split()[0] for line in lines[-4:]]
        self.assertEqual((len(set(scripts)), scripts[2] == scripts[3]), (3, True))

    def test_a_run_query_matches_task_and_run_ids_never_the_homes_own_path(self) -> None:
        from opsquire.query import find_run

        self.fixture_run("20261101-1000_web1_first", "2026-11-01T10:00:00.000Z", "ok", 1.0)
        with mock.patch.dict(os.environ, self.env):
            self.assertIsNone(find_run(self.tmp.name), "a word of the home's path names no run")
            found = find_run("web1_first")
            self.assertEqual(found.name if found else None, "20261101T100000000Z_read")
