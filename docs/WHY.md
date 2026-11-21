# Why Opsquire

AI coding agents are good at operational preparation: they find the right script, write the missing one, test it
and work out the order of steps. They should not be the ones who run it against production. Opsquire splits the two
jobs and keeps a record of both.

- **The agent prepares.** It writes a task into the home (a script, its tests, its targets and how to undo it) and
  hands over one command.
- **You run it.** `opsquire run <task>` does the dry run, and `--apply` does the change. Scripts ask before their first
  change; lint flags one that never asks, and the console then takes a second click. You can do the same from the
  console's inbox in the browser.
- **The next agent reads the result.** Every run leaves a structured record (status per target, errors, results,
  logs) that the agent waits for and continues from. You never paste output back into a chat.

The result is that you get an agent's speed for the preparation, and nothing reaches production without your
command.

## What it gives you

| Need | How Opsquire meets it |
|---|---|
| Nothing changes without a person | Agents cannot write run records or read credentials (their sandbox denies both). Only your command runs a task. |
| No surprise applies | An apply is refused until a dry run of the same scripts and arguments finished ok. |
| Nothing half-written reaches you | A task waits on you only once its agent hands it over; an edit after that takes it back. Every run first lists the files it executes, each linked to its exact copy, with what changed since the last run. |
| Who approved what | A hash-chained ledger of every run and decision, with Merkle checkpoints. `opsquire audit` proves nothing changed, and a checkpoint command in Settings sends each checkpoint off the machine. |
| Less ceremony where it is safe | Tiers by target (`local`, `dev`, `staging`, `prod`). Batch-start every read and dry run in one click. Optionally let the console auto-run chosen modes outside prod, under rules only you can seal. |
| Reuse instead of rewriting | `opsquire find` ranks shared scripts and past tasks. Tasks call shared scripts instead of copying them. |
| Two runs never collide | Locks per target (host or a part of one) that only changes hold exclusively, a line that blocked runs wait in, fencing tokens and plans that lock all their targets up front. |
| Clean interrupts | Ctrl-C at any point leaves an `interrupted` record and releases locks. Nothing else is left behind. |

## Two minutes, on Linux or a Mac

```
opsquire init ~/ops-home                 # home, local rules, config, credentials file, agent skill, a first task
opsquire run demo_hello                  # the first task: a read-only run with a full record
opsquire last                            # what just happened, as a person reads it
opsquire last --json                     # ... and as the next agent reads it
opsquire ui --open                       # the console: the inbox, running jobs, history, Settings
```

Then ask for something real, in the terminal or in the console's Ask box:

```
opsquire ask "check disk usage on web1 and web2, and clean the old logs if any is over 80%"
```

The agent searches for an existing script, writes the task (a read of both hosts, then a changing task with a dry
run), lints and tests it, and ends with the commands. You run the read, then the dry run, look at the plan it
prints, and apply it. The agent waits for each record and reports what changed.

## Who it is for

- **Anyone who lets agents near real systems:** servers, deploys, databases, config repos. If you already copy an
  agent's commands into a terminal, Opsquire turns that copy-paste into reviewed, recorded and reusable runs.
- **Small teams that need an audit trail of AI-assisted changes,** without building one.
- **On-call work:** an agent diagnoses and prepares the fix while you decide, and the record explains it the next
  morning.

It is overkill for purely local coding. Your agent's normal tools are enough for that.

## What it does not do

- **It sets up only the agents it starts.** The guarantee that records are real depends on agents being denied
  writes to the home's `var/` and reads of the credentials file. Opsquire sets that for the agents `opsquire ask`
  starts and stops any whose first-step `opsquire doctor --boundary` says otherwise; any other agent (a chat's)
  needs its own sandbox set up that way, and the same command, run from inside it, checks it.
- **Tiers trust the targets a task declares.** Auto-run is off until you turn it on, it never applies to `prod`, and
  it is safe only where a wrong target cannot hurt. Its rules count only once sealed in `var/`, the same place the
  agent's sandbox must keep it out of.
- **It never merges, deploys or changes your environment by itself.** Every change to a live system is a run you
  started, or one your own auto-run rule allowed: an apply from cron or a script follows those rules too, so it never
  reaches `prod`.
