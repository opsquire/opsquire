# Opsquire

[![Checks](https://github.com/opsquire/opsquire/actions/workflows/checks.yml/badge.svg)](https://github.com/opsquire/opsquire/actions/workflows/checks.yml)

Agents prepare the scripts, you run them, and every run is recorded for the next agent.

A squire prepares the arms; the knight acts. Opsquire lets AI coding agents do the preparation
for operational work (finding or writing the right script, linting it, planning the order)
while you stay the one who runs anything that touches production, with one command per step.
Every run leaves a structured, agent-readable record that agents cannot forge: agents are denied writes to the home's
`var/` and reads of the credentials file. For the agents `opsquire ask` starts, opsquire sets that itself and stops
any agent whose `opsquire doctor --boundary` check, its required first step, does not say the boundary is kept. Any
other agent (a chat's) needs its own sandbox set up that way; the same command, run from inside it, checks it.

New here? [docs/WHY.md](docs/WHY.md) says what it is for, what it guarantees and what it does not, with a two-minute
walkthrough.

**Status: early.** One operator has used it daily since October 2026; nobody else has installed it yet. Expect
changes between minor versions, and read [SECURITY.md](SECURITY.md) before you point it at production.

## Install

From the latest release (a wheel attached to the GitHub release, fetched with `gh`). Opsquire
needs Python 3.13 or newer; `uv` brings its own, so this works on any Linux (or WSL) or Mac, whatever Python it ships:

```
curl -LsSf https://astral.sh/uv/install.sh | sh             # uv, if you do not have it; then open a new terminal
sudo apt install -y gh git shellcheck bubblewrap             # Debian, Ubuntu
brew install gh git shellcheck bash                          # macOS (tests are isolated by its built-in sandbox-exec)
gh auth login
gh release download --repo opsquire/opsquire --pattern '*.whl' --dir /tmp/opsquire
uv tool install --python 3.13 --with ruff --with mypy /tmp/opsquire/*.whl
opsquire init ~/ops-home --budget 15     # one command: home, local rules, config, credentials, skill, first task
opsquire run <the first task it printed>
```

With pipx instead, on a system whose Python is already 3.13 or newer:
`pipx install --python python3.13 /tmp/opsquire/*.whl && pipx inject opsquire ruff mypy`. Upgrading later is the same
`uv tool install` with `--force`.

Times show in UTC until you choose: `opsquire init --time-zone local` (or a zone name such as `America/New_York`,
or the console's Settings) shows them in yours. Records and `--json` stay UTC.

`ruff` and `mypy` are the lint gate every Python task must pass before it runs (`shellcheck` for shell tasks), so a
home without them can run nothing. The unit tests agents write run only inside a `bubblewrap` sandbox (on a Mac, the
built-in `sandbox-exec`); where neither works (`opsquire doctor` says so), lint runs no test and fails, unless you
accept weaker isolation by running opsquire with `OPSQUIRE_TEST_ISOLATION=weaker-ok` in your own shell's environment: yours to set, never an agent's. Scripts run on opsquire's own Python, so the libraries they import must be
installed next to it: list them in the home's `requirements.txt` and add `--with-requirements <home>/requirements.txt`
to every `uv tool install` (a reinstall without it drops them; `opsquire doctor` names any that are missing). `opsquire init` is safe to run again: it never overwrites what exists, and it
reports every step, including any tool that is missing. It writes
`LOCAL.md` in the home from a template: edit it, it is where this home's rules live (who the operator is, what
counts as a task here, areas, credentials, tools). Credentials go in `~/.creds` (or `$OPSQUIRE_CREDS`) as JSON;
tasks declare what they need by key and get it as environment variables, and the runner redacts the values from
every record. Agents never read the file.

From a checkout, for development:

```
uv tool install --python 3.13 --editable . --with ruff --with mypy --with-requirements <home>/requirements.txt
```

## Your first hour

1. Install as above. On a Mac that is Homebrew's `gh`, `git`, `shellcheck` and `bash`, plus `uv`.
2. `opsquire init ~/ops-home --time-zone local`: the home, its local rules (`LOCAL.md`), your config, an empty
   credentials file, the opsquire skill and Stop hook for Claude Code, and a first read-only task. It prints what to
   run next and names anything missing.
3. Run that first task with the command it printed, then read what it recorded: `opsquire last`.
4. `opsquire doctor`: everything it marks to fix, fix now. It is quick to run again.
5. `opsquire ui --open` opens the console; log in with the link it prints. Keep it running while you work.
6. Ask for something real but harmless, in the console's Ask box or with `opsquire ask "how much disk is free on this
   machine"` (this needs Claude Code installed and logged in). The agent writes the task, lints it and hands it over.
   It appears in the Inbox, and you run it there.
7. Working from your own agent chat instead? Open it anywhere: the skill gives it the rules. Have it run
   `opsquire doctor --boundary` once from inside its sandbox. Anything but "boundary kept" means its sandbox can
   forge records or read credentials, so fix that before it writes a task.
8. Edit `LOCAL.md` in the home: who you are, what counts as a task here, your areas, and the names of the
   credentials tasks may ask for (never the values).

## Daily use

```
opsquire ask "<what you want>"     an agent finds or writes the task and prints what to run
opsquire find <words>              is there already a script for this?
opsquire run <plan>                several tasks in order, one command (dry first, then --apply)
opsquire run <task>                dry run (or read-only run); first lists every file it executes, each linked
                                   to the run's exact copy (Ctrl+click opens it), new, changed or as last run
opsquire run <task> --apply        the real thing; asks before each change; refused until a dry run of the
                                   same scripts and arguments finished ok (--skip-dry "why" overrides, on record)
opsquire run <task> --apply --dry-first   the dry run, then the apply only if it ended ok, in one command
                                   (the console's "Dry run, then apply"); the apply still asks before changing
opsquire run <task> --no-wait      end blocked when another run holds a target, instead of waiting in line
opsquire inbox                     what waits on you: each task's next run with its command, and unread chats
opsquire last                      what just happened
opsquire runs --status failed      what went wrong, across everything
opsquire log <task>                full output of the latest run
opsquire wait <task>               (for agents) block until your next run of a task finishes
opsquire handover <task> [--apply] --why "..." [--changed "..."]   (for agents) the task is ready: only now does it
                                   reach the inbox (and auto-run); an edit after it takes it out again
opsquire locks                     the locks held now: target, mode, run, task, user, since, still alive
opsquire unlock <target>           remove every lock on a target (asks you to type it)
opsquire audit                     check the tamper-evident ledger of runs and decisions, and every run record
                                   against it; prints a checkpoint to keep off this machine, which
                                   `opsquire audit --against <checkpoint>` later proves was not rewritten (a
                                   checkpoint command in Settings sends each new one by itself)
opsquire policy                    tiers by target (local, dev, staging; the rest is prod) and what the console
                                   may start by itself per tier; --tier dev 'dev-*', --auto dev read dry, --auto dev off
opsquire hold <task> --why "..." [--until 15:00]   prepared for later: out of the inbox until handed over
opsquire supersede <task> --by <newer>   a waiting task that will not run as it is; `reopen` undoes it
                                   (inbox items waiting 3+ days ask for this; tasks that never ran expire in 14)
opsquire offboard --due            settled tasks idle for 7 days leave tasks/ for offboarded/ (also daily, on its own)
opsquire restore <task>            bring one back; `opsquire init --offboard-days N` sets the retention
opsquire compact                   share repeated snapshot files and gzip logs of runs finished for a day
                                   (also daily, with the offboard sweep)
opsquire tend                      library upkeep: how much each general script is used, code tasks keep copying,
                                   scripts loading shared code by hand, scripts nothing used for 30 days;
                                   --fix hands the fixable part to an agent (an ask, stops at $10; the console's
                                   Library tab has the same report and button)
opsquire milestones --since 2h     what others would want to know: tasks closed, applies or verifies that failed,
                                   thread results
opsquire doctor                    console up and current, stop hook, libraries scripts import, test isolation,
                                   a shared home's group, contract, author: what needs fixing
opsquire share <group>             share the home with other operators (see Sharing a home between operators)
opsquire ui --open                 the console: the inbox of runs waiting on you, threads, runs, locks, and an
                                   Ask box: your request goes to an agent as a chat you can follow and answer;
                                   a chat with a reply you have not opened counts on the Inbox tab and the page
                                   title until you open it (read state is yours, var/chats-read/<user>.json);
                                   it logs a browser in through a link it prints (valid until the console
                                   stops; a restart ends every login)
opsquire integrate                 install the opsquire skill and Stop hook for Claude agents; make the home a
                                   local-only git repo
opsquire ask --no-split "<...>"    one agent only (asks split into parallel pieces by default)
opsquire ask --max-agents 2 ...    at most 2 agents at once; --budget 20 [--strict-budget] for the spend
opsquire ask --model quick ...      a tier the agent's retinue entry defines (quick, default, deep) or a model id;
                                   --agent picks another agent from retinue.json. Spend is what the agent reports
                                   at API list prices; its retinue entry says whether that is a plan or an API key
opsquire ask --resume <id> --fresh "..."   continue a chat in a new agent session briefed from its records; the
                                   console does this for follow-ups once a chat turn cost $5 (Settings changes it)
opsquire repo checkout|pr ...      (for agents) change a registered repo through a GitHub-signed PR
```

What ask agents may use beyond their retinue's allowed tools is set in the console's Settings (the home's
`tools.json`): each server the agent has is off until you switch it on, and each tool, shell command or pattern is
Allow, Confirm or Off. Confirm puts Allow and Deny in the ask's chat (or asks on the terminal) and waits; with nobody
to answer it is denied. Shell commands without a rule ask only when they reach out: talk to another machine or
service (ssh, scp, curl, git push, gh writes, opsquire run) or act as someone else (sudo); edits, tests and lint in
the agent's own copy run. Settings can make it ask for everything but read-only commands, or for everything. When
several agents of a split ask wait at once, the chat lists every request, each marked reads only or changes, with
Allow all read-only. Servers added in Settings reach ask agents only. A home's `retinue.json` is laid over the
bundled one, so it names only what it changes.

Tiers decide how much ceremony a run needs. They live in your config file, and `opsquire policy` or the console's
Settings change them. Either one also seals the rules in the home's `var/`, where agents cannot write, and auto-run
follows only sealed rules: a change from anywhere else (an agent's shell, an editor) shows as NOT SEALED and starts
nothing until you run `opsquire policy --seal`. Each seal is a ledger entry. The inbox shows each item's tier, and "Run all reads and dry runs" starts every waiting run
that changes nothing. Auto-run is off until you switch it on for a tier and mode. The console then starts those runs
itself, as your runs, and each record names the rule. It trusts the targets each task declares, and `prod` never
auto-runs. A command line with nobody at it (cron, a script) follows the same rules for applies: `opsquire run --apply`
from cron is refused unless the task's tier auto-runs apply. Reads, dry runs and verify checks run unattended anywhere.

Ctrl-C is always safe: the run is recorded as `interrupted`, scratch work is undone, locks are
released, and nothing else is left behind.

The full contract agents follow is in [src/opsquire/data/AGENTS.md](src/opsquire/data/AGENTS.md);
`opsquire contract --write` installs it, plus the home's `LOCAL.md` rules, as the home's `AGENTS.md`, and opsquire
keeps that file current afterwards; it never overwrites an edit made in it by hand (it says to move it into `LOCAL.md`).

## Sharing a home between operators

Several people on one Linux machine, each with their own account, can work in one home: one inbox, one set of tasks,
one run history, one ledger, and locks that hold across all of them (a Mac drops the setgid bit this relies on). Create
a group with the system's own tools, add
each operator to it, then, once, from the account that set the home up (each operator logs in again first, so their
shell has the group):

```
sudo groupadd ops && sudo usermod -aG ops alice && sudo usermod -aG ops bob     # or your system's equivalent
opsquire share ops --dry      # what would change
opsquire share ops            # the group gets read and write on everything in the home, folders get setgid
```

Each operator then points opsquire at the home (`opsquire home <path>` or `OPSQUIRE_HOME`). From then on opsquire
creates everything there group-readable and group-writable; it changes nothing outside the home, and opsquire
itself never runs `sudo` or touches groups. Records name the account that ran them (`user` and `uid`, taken from
the process, not from `$USER`), and git works in the home whoever created it, without any git config change. Each
operator keeps their own credentials file (`~/.creds`), their own agents and agent sandbox, and their own console
on a port of their own (`opsquire ui --port 7772`). `opsquire doctor` names the group and anything in the home the
group cannot read and write (a file an editor or an agent's own tools wrote with a stricter umask, say);
`opsquire share ops`, run by whoever owns those files, fixes them.

## Development

```
pip install -e ".[lint,test]"
bash scripts/ci.sh                       # every check below, as CI runs them: lint, tests, e2e, package
python -m pytest -q -n auto tests        # every test module, in parallel (a few minutes)
python -m pytest -q tests/test_plans.py  # one area: tests/test_<area>.py, helpers in tests/support.py
ruff check . && ruff format --check .
mypy src tests scripts
vulture                                  # dead code; hooks it cannot see called are named in pyproject.toml
python scripts/clean_text.py EVENT_JSON  # words that never belong here: a private list, never in the repository
(cd src/opsquire/data/helpers && mypy --config-file ../mypy.ini opsquire_helpers.py)
(cd src/opsquire/data/templates && MYPYPATH=../helpers mypy --config-file ../mypy.ini main.py verify.py read.py)
shellcheck -x -P src/opsquire/data/helpers src/opsquire/data/helpers/opsquire.sh src/opsquire/data/templates/*.sh
python scripts/full_power.py   # every feature end to end, through the real CLI, in a scratch home
```

Coverage counts every opsquire process the tests start (most of them run the CLI), with `pip install -e ".[coverage]"`:

```
export COVERAGE_FILE=/tmp/opsquire-cov/.coverage && mkdir -p /tmp/opsquire-cov
python -m coverage run -m pytest -q -n auto tests
python -m coverage combine /tmp/opsquire-cov && python -m coverage report
```

CI does the same on Python 3.13 and puts the table in the run's summary.

Standard library only, Python 3.13+. `scripts/ci.sh` holds every check: lint, the test suite, the end-to-end
script, and building the wheel, installing it into an empty venv and checking that every data file ships. Run it
before pushing. [CI](.github/workflows/ci.yml) runs it before every release, with the test suite on the oldest and
newest supported Python and on macOS, and anyone can start it by hand from the Actions tab.

Commits reach `main` as commits GitHub signs itself, never through `git push`:

```
git add <files>
python3 scripts/signed_commit.py -m "area: what changed" [-m "why, in a paragraph"]
```

It sends the staged files with `createCommitOnBranch`, refuses when the checkout is behind `origin/main`, and
moves the local branch onto the new commit. Subjects are `area: summary` (`ui:`, `repo:`, `close:` ...); the
body says why. Release notes are built from them.

## Releases

Opsquire is developed in a private repository and published here one release at a time, without its history. A
maintainer publishes with:

```
python3 scripts/export.py --bump minor --dry    # what would go out: files, release notes, the word-list check
python3 scripts/export.py --bump minor          # set the version, then the GitHub-signed export commit(s)
gh workflow run release.yml --repo opsquire/opsquire
```

[`scripts/export.py`](scripts/export.py) refuses unless the maintainer's private word list is present and finds
nothing in the files or the release notes, and when this repository's `main` holds changes no export made (an
accepted pull request is brought into the private repository first). The [release workflow](.github/workflows/release.yml)
runs the full checks, macOS included, tags the version the export brought, builds the sdist and wheel from the tag,
and publishes a GitHub release with the export's notes and the wheel attached. `opsquire --version` shows the
installed version. Nothing is published outside GitHub. Every push and pull request here runs the
[checks](.github/workflows/checks.yml).

## Security

Report a vulnerability privately, as [SECURITY.md](SECURITY.md) says.

## License

[Apache-2.0](LICENSE).
