# Security

Opsquire sits between AI agents and the commands that change production, so a hole in it can let an agent act
without a person. Report one privately, never in a public issue: use **Report a vulnerability** on the repository's
Security tab (GitHub private vulnerability reporting). Expect a first answer within a week.

## What counts

Anything that breaks one of the guarantees in [docs/WHY.md](docs/WHY.md), for example:

- an agent writing run records or the ledger under `var/`, or reading the credentials file, despite the boundary
  check (`opsquire doctor --boundary`);
- a run starting without the operator's command, or an apply without the dry run it needs;
- `opsquire audit` passing over a rewritten record or ledger entry;
- a credential value reaching a run record, a log, lint output or the console page unmasked;
- the console serving anything to a browser that did not log in with its link, or accepting a request from another
  site;
- a task's tests reaching the home, the credentials file or the network from inside their sandbox.

## Supported versions

Only the latest release gets fixes.
