"""L1-L6: what a turn must prove before its answer reaches anyone.

A turn is one question from one person and everything done to answer it:
the token they arrived with, the credential the work ran under, each query
and its result, each chart saved or dashboard edited, and the prose answer.
The broker writes it to `turn.json`; this module grades that file. It
shares no objects with the code that produced it, and it re-executes every
query itself - as the asking person - instead of trusting the recorded result.

| rule | refuses |
|---|---|
| L1 identity   | a token that doesn't verify (signature, issuer, audience, validity at the time asked) or whose groups map to no role |
| L2 credential | work done under anything but the asking person's own session - an admin key, a service account, someone else's session |
| L3 read scope | a query on a connection the role isn't granted, or one that reads a table outside that connection's schemas, or isn't a single read-only SELECT |
| L4 write scope| a chart saved, or a dashboard edited, in a collection the role can't curate |
| L5 numbers    | a recorded result that differs from re-running it as the person; any number in the answer that no verified result (or the question) contains |
| L6 sync       | a person whose Metabase groups differ from what their directory groups say today - stale access in either direction |
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import numbers, sqlscope
from .backend import Credential
from .idp import Identity, IdentityError, parse_time, verify
from .policy import Policy

RULES = ["L1", "L2", "L3", "L4", "L5", "L6"]
NAMES = {"L1": "identity", "L2": "credential", "L3": "read scope", "L4": "write scope", "L5": "numbers", "L6": "sync"}


@dataclass
class Verdict:
    results: dict[str, list[str]] = field(default_factory=dict)  # rule -> problems ([] = pass)
    skipped: list[str] = field(default_factory=list)
    identity: Identity | None = None

    @property
    def failed(self) -> list[str]:
        return [r for r in RULES if self.results.get(r)]

    @property
    def passed(self) -> bool:
        return not self.failed and not self.skipped

    def to_json(self) -> dict:
        return {
            "passed": self.passed, "failed": self.failed, "skipped": self.skipped,
            "problems": {r: p for r, p in self.results.items() if p},
            "identity": None if self.identity is None else
            {"email": self.identity.email, "roles": list(self.identity.roles)},
        }


# --- per-call checks, shared with the broker so it can refuse before acting ---

def read_problems(policy: Policy, identity: Identity, connection: str, sql: str) -> list[str]:
    granted = policy.connections_for(list(identity.roles))
    if connection not in granted:
        return [f"connection {connection} is not granted to {', '.join(identity.roles)} (has {sorted(granted)})"]
    return sqlscope.check(sql, policy.schemas_for(connection))


def write_problems(policy: Policy, identity: Identity, call: dict, backend, saved: set[str]) -> dict[str, list[str]]:
    """{"L3": [...], "L4": [...]} - a saved question is also a read, so it answers to both."""
    curate = policy.curate_for(list(identity.roles))
    who = ", ".join(identity.roles)
    if call["tool"] == "save_question":
        l4 = [] if call["collection"] in curate else [
            f"collection '{call['collection']}' is not curatable by {who} (can curate {sorted(curate)})"]
        return {"L3": read_problems(policy, identity, call["connection"], call["sql"]), "L4": l4}
    if call["tool"] == "add_to_dashboard":
        coll = backend.dashboard_collection(call["dashboard"])
        if coll is None:
            return {"L3": [], "L4": [f"no dashboard '{call['dashboard']}'"]}
        l4 = [] if coll in curate else [f"dashboard '{call['dashboard']}' lives in '{coll}', not curatable by {who}"]
        if call["card"] not in saved:
            l4.append(f"card '{call['card']}' was not saved in this turn")
        return {"L3": [], "L4": l4}
    return {"L3": [], "L4": [f"unknown write tool {call['tool']}"]}


def sync_problems(policy: Policy, identity: Identity, backend) -> list[str]:
    want = {policy.roles[r].metabase_group for r in identity.roles}
    have = set(backend.memberships(identity.email))
    p = []
    if have - want:
        p.append(f"Metabase still grants {sorted(have - want)} that the directory no longer does")
    if want - have:
        p.append(f"directory grants {sorted(want - have)} that Metabase does not have yet")
    return p


# --- the whole turn -----------------------------------------------------

def grade(turn: dict, backend, policy: Policy, jwks: dict) -> Verdict:
    v = Verdict()
    asked = parse_time(turn["asked_at"])

    # L1 - nothing else is meaningful without a verified person.
    try:
        ident = verify(turn["token"], jwks, policy, asked)
        v.identity = ident
        v.results["L1"] = []
    except IdentityError as e:
        v.results["L1"] = [str(e)]
        v.skipped = [r for r in RULES if r != "L1"]
        return v

    # L2 - the work ran as this person, not as something with more reach.
    cred = turn.get("credential") or {}
    p2 = []
    if cred.get("kind") != "user-session":
        p2.append(f"work ran under credential kind '{cred.get('kind') or 'missing'}' ({cred.get('principal')}), not the person's own session")
    elif cred.get("principal") != ident.email:
        p2.append(f"work ran under {cred.get('principal')}'s session, but {ident.email} asked")
    if cred.get("principal") in policy.service_principals:
        p2.append(f"{cred.get('principal')} is a service principal")
    v.results["L2"] = p2

    # L3 / L4 - every executed call, in order.
    p3, p4, verified_reads, saved = [], [], [], set()
    for n, call in enumerate(turn["calls"], 1):
        if call.get("refused"):
            continue  # refused by the broker before it ran: nothing executed, nothing to grade
        if call["tool"] == "run_sql":
            probs = read_problems(policy, ident, call["connection"], call["sql"])
            p3 += [f"call {n}: {p}" for p in probs]
            if not probs:
                verified_reads.append((n, call))
        elif call["tool"] in ("save_question", "add_to_dashboard"):
            probs = write_problems(policy, ident, call, backend, saved)
            p3 += [f"call {n}: {p}" for p in probs["L3"]]
            p4 += [f"call {n}: {p}" for p in probs["L4"]]
            if call["tool"] == "save_question":
                saved.add(call["name"])
        else:
            p3.append(f"call {n}: unknown tool {call['tool']}")
    v.results["L3"], v.results["L4"] = p3, p4

    # L5 - re-run each verified read as the person, then trace every number.
    p5, cells = [], []
    me = Credential("user-session", ident.email)
    for n, call in verified_reads:
        try:
            fresh = backend.run_native(me, call["connection"], call["sql"])
        except Exception as e:  # noqa: BLE001 - any failure to reproduce is a refusal
            p5.append(f"call {n}: could not re-run as {ident.email}: {e}")
            continue
        if json.loads(json.dumps(fresh)) != json.loads(json.dumps(call.get("result"))):
            p5.append(f"call {n}: recorded result differs from re-running it as {ident.email}")
            continue
        cells += [c for row in fresh["rows"] for c in row]
    available = numbers.sources(cells) | numbers.sources([turn["question"]])
    stray = numbers.unsourced(turn["answer"], available)
    if stray:
        p5.append(f"answer contains number(s) no verified result supplied: {', '.join(stray)}")
    for n, call in enumerate(turn["calls"], 1):  # a chart title is prose people read, too
        if call["tool"] == "save_question" and not call.get("refused"):
            stray = numbers.unsourced(call["name"], available)
            if stray:
                p5.append(f"call {n}: chart title contains unsourced number(s): {', '.join(stray)}")
    v.results["L5"] = p5

    # L6 - Metabase agrees with the directory about who this is.
    v.results["L6"] = sync_problems(policy, ident, backend)
    return v


def grade_dir(path: Path, backend, policy: Policy, jwks: dict) -> Verdict:
    turn = json.loads((path / "turn.json").read_text(encoding="utf-8"))
    v = grade(turn, backend, policy, jwks)
    (path / "verdict.json").write_text(json.dumps(v.to_json(), indent=2), encoding="utf-8")
    return v
