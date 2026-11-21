"""Tiers and auto-run: how much of the operator's ceremony a run needs, by where it lands.

The operator's config file names target patterns per tier (`local`, `dev`, `staging`, `prod`). A target no pattern
names is `prod`, and a task takes the strictest tier of its targets. For each tier but `prod` the operator may switch
on auto-run for some modes: the console then starts a waiting inbox item of that tier and mode by itself, as the
operator's own job, and its run record names the rule (`policy`). Auto-run trusts the targets a task declares, so it
is off everywhere until the operator turns it on, and `prod` never auto-runs. An apply started with nobody present
(cron, a script) follows the same rules: the runner refuses it unless its tier auto-runs apply.

An agent's sandbox may well be able to write the config file, so the rules count for auto-run only while they match
the seal in the home's `var/` (written by the operator's own `opsquire policy` or the console's Settings, where agents
cannot write): rules changed any other way show in the console but start nothing until the operator seals them. Each
seal is a ledger entry."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from typing import Any

from . import ledger
from .model import OpsquireError, config_path, iso, now, read_json, update_config, user_name, var_dir, write_json

TIERS = ("local", "dev", "staging", "prod")
AUTO_TIERS = TIERS[:-1]
AUTO_MODES = ("read", "dry", "verify", "apply")
STRICTEST = TIERS[-1]
POLICY_ENV = "OPSQUIRE_POLICY"
CONFIG_KEY = "policy"
SEAL = "policy-seal.json"


def settings() -> dict[str, Any]:
    """The operator's policy, normalised: {"tiers": {tier: [pattern, ...]}, "auto_run": {tier: [mode, ...]}}."""
    try:
        raw = read_json(config_path()).get(CONFIG_KEY)
    except (OSError, ValueError, AttributeError):
        raw = None
    given: dict[str, Any] = raw if isinstance(raw, dict) else {}
    tiers = given.get("tiers") if isinstance(given.get("tiers"), dict) else {}
    auto = given.get("auto_run") if isinstance(given.get("auto_run"), dict) else {}
    rules = {
        "tiers": {t: [str(p) for p in (tiers or {}).get(t) or [] if str(p).strip()] for t in AUTO_TIERS},
        "auto_run": {t: [m for m in AUTO_MODES if m in ((auto or {}).get(t) or [])] for t in AUTO_TIERS},
    }
    return {**rules, "sealed": sealed(rules)}


def digest(rules: dict[str, Any]) -> str:
    body = {"tiers": rules["tiers"], "auto_run": rules["auto_run"]}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def sealed(rules: dict[str, Any]) -> bool:
    """Whether these rules are the ones the operator last sealed in var/."""
    try:
        seal = read_json(var_dir() / SEAL)
    except (OSError, ValueError, OpsquireError):
        return False
    return isinstance(seal, dict) and seal.get("digest") == digest(rules)


def seal(rules: dict[str, Any]) -> bool:
    """Seal the rules in var/ and the ledger; False where var/ cannot be written (an agent sandbox)."""
    mark = {"digest": digest(rules), "at": iso(now()), "user": user_name()}
    try:
        var_dir().mkdir(parents=True, exist_ok=True)
        write_json(var_dir() / SEAL, mark)
    except (OSError, OpsquireError):
        return False
    ledger.append("policy", {**mark, "tiers": rules["tiers"], "auto_run": rules["auto_run"]})
    return True


def validate(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict) or set(policy) - {"tiers", "auto_run"}:
        raise OpsquireError('policy is {"tiers": {tier: [patterns]}, "auto_run": {tier: [modes]}}')
    tiers, auto = policy.get("tiers", {}), policy.get("auto_run", {})
    for name, value, allowed in (("tiers", tiers, None), ("auto_run", auto, AUTO_MODES)):
        if not isinstance(value, dict):
            raise OpsquireError(f"policy {name} must be an object keyed by tier")
        for tier, items in value.items():
            if tier not in AUTO_TIERS:
                raise OpsquireError(f"policy {name}: {tier!r} is not one of {', '.join(AUTO_TIERS)} (prod is the rest)")
            if not isinstance(items, list) or not all(isinstance(i, str) and i.strip() for i in items):
                raise OpsquireError(f"policy {name}.{tier} must be a list of non-empty strings")
            if allowed and set(items) - set(allowed):
                raise OpsquireError(f"policy auto_run.{tier}: modes are {', '.join(AUTO_MODES)}")
    return {"tiers": tiers, "auto_run": auto}


def change(update: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial policy (tiers and/or auto_run, per tier) into the config file, seal it, and return the result
    (`sealed` false when this process cannot write var/). An empty update seals the rules as they stand."""
    validate(update)
    current = settings()
    merged = {
        "tiers": {**current["tiers"], **update.get("tiers", {})},
        "auto_run": {**current["auto_run"], **update.get("auto_run", {})},
    }
    if update:
        update_config(**{CONFIG_KEY: merged})
    seal(merged)
    return settings()


def tier_of(target: str, policy: dict[str, Any] | None = None) -> str:
    """The strictest tier whose patterns match the target or the host it is a part of; `prod` when none does."""
    policy = policy or settings()
    names = {target, target.split("/", 1)[0]}
    matched = [t for t in AUTO_TIERS if any(fnmatch.fnmatchcase(n, p) for p in policy["tiers"][t] for n in names)]
    return max(matched, key=TIERS.index) if matched else STRICTEST


def task_tier(targets: list[str], policy: dict[str, Any] | None = None) -> str:
    policy = policy or settings()
    return max((tier_of(t, policy) for t in targets), key=TIERS.index, default=STRICTEST)


def auto_allowed(tier: str, mode: str, policy: dict[str, Any] | None = None) -> bool:
    policy = policy or settings()
    return bool(policy.get("sealed")) and tier != STRICTEST and mode in policy["auto_run"].get(tier, [])


def started_by() -> dict[str, str] | None:
    """The rule that started this run, when the console started it by policy (run.json `policy`)."""
    raw = os.environ.get(POLICY_ENV, "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    return {"tier": str(value.get("tier", "")), "mode": str(value.get("mode", "")), "by": "auto-run"}


def env_for(tier: str, mode: str) -> dict[str, str]:
    return {POLICY_ENV: json.dumps({"tier": tier, "mode": mode})}
