"""python -m lensgate <command> - see README for the walkthrough."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import gate
from .policy import Policy


def _wrap(text: str, width: int = 96, indent: str = "      ") -> str:
    import textwrap
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


def cmd_seed(a):
    from .seed import seed
    print(seed(a.data))


def cmd_ddl(a):
    from .seed import ddl, roles_sh
    sys.stdout.write(roles_sh(Policy.load()) if a.roles else ddl() + "\n")


def cmd_demo(a):
    from . import demo
    out = Path(a.out)
    results = demo.run(Path(a.data), out)
    delivered = 0
    for r in results:
        rec, v = r["record"], r["verdict"]
        who = v.identity.email.split("@")[0] if v and v.identity else "?"
        status = "PASS" if v and v.passed else "FAIL"
        calls = rec["calls"]
        ran = sum(1 for c in calls if not c.get("refused"))
        refused = [c["refused"] for c in calls if c.get("refused")]
        print(f"{status}  {rec['turn_id']}  {who:<13} {r['outcome']}  ({ran} call(s) ran, {len(refused)} refused)")
        print(_wrap("Q: " + rec["question"]))
        for x in refused:
            print(_wrap("refused: " + x))
        print(_wrap("A: " + r["delivered"]))
        delivered += r["outcome"].startswith("delivered")
    writes = json.loads((out / "metabase_writes.json").read_text(encoding="utf-8"))
    full = sum(1 for r in results if r["outcome"] == "delivered")
    declined = sum(1 for r in results if r["outcome"].startswith("delivered,"))
    print(f"\n{len(results)} turns: {full} answered in full, {declined} declined in scope "
          f"(the out-of-scope query never ran), {len(results) - full - declined} withheld. "
          f"{len(writes)} Metabase write(s), each as the person who asked:")
    for w in writes:
        tgt = w.get("collection") or w.get("dashboard")
        print(f"  {w['op']:<17} {w.get('name') or w.get('card')}  ->  {tgt}  (by {w['by'].split('@')[0]})")
    sys.exit(0 if all(r["verdict"] and r["verdict"].passed for r in results) else 1)


def cmd_check_dir(a):
    from .backend import LocalBackend
    from .idp import DevIdP
    policy = Policy.load()
    backend = LocalBackend(a.data, policy)
    jwks = DevIdP(a.data, policy).jwks()
    root = Path(a.dir)
    dirs = sorted(p.parent for p in root.rglob("turn.json"))
    if not dirs:
        print(f"no turn.json under {root}")
        sys.exit(2)
    bad = 0
    for d in dirs:
        v = gate.grade_dir(d, backend, policy, jwks)
        if v.passed:
            print(f"PASS  {d.name}")
            continue
        bad += 1
        own = re.match(r"(l\d)-", d.name)
        tag = ""
        if own:
            want = own.group(1).upper()
            tag = "  (own rule)" if v.failed == [want] else f"  (COLLATERAL: failed {v.failed}, expected [{want}])"
        print(f"FAIL  {d.name}{tag}")
        for r in v.failed:
            for p in v.results[r]:
                print(_wrap(f"{r} {gate.NAMES[r]}: {p}"))
        if v.skipped:
            print(_wrap(f"not evaluated without a verified identity: {', '.join(v.skipped)}"))
    print(f"\n{len(dirs) - bad}/{len(dirs)} passed")
    sys.exit(1 if bad else 0)


def cmd_counterexamples(a):
    from .counterexamples import DESCRIPTIONS, build
    names = build(Path(a.data), Path(a.out))
    for n in names:
        print(f"{n:<28} {DESCRIPTIONS[n]}")


def cmd_sync(a):
    from .idp import DevIdP
    policy = Policy.load()
    directory = list(DevIdP(a.data, policy).directory.values())
    if a.metabase:
        from .metabase import sync_live
        drift = sync_live(a.metabase, policy, directory, a.apply)
        sys.exit(1 if drift and not a.apply else 0)
    path = Path(a.data) / "metabase" / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    drift = []
    for p in directory:
        want = {policy.roles[r].metabase_group for r in policy.roles_for_groups(p["groups"])}
        have = set(state["users"].get(p["email"], {}).get("groups", []))
        for g in sorted(have - want):
            drift.append(f"{p['email']}: stale {g} (directory no longer grants it)")
        for g in sorted(want - have):
            drift.append(f"{p['email']}: missing {g}")
        if a.apply:
            state["users"].setdefault(p["email"], {"name": p["name"]})["groups"] = sorted(want)
    for line in drift:
        print(("fixed  " if a.apply else "drift  ") + line)
    if a.apply:
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"{len(drift)} difference(s) between the directory and Metabase" + (" - fixed" if a.apply and drift else ""))
    sys.exit(1 if drift and not a.apply else 0)


def cmd_token(a):
    from .idp import DevIdP
    now = datetime.now(timezone.utc)
    print(DevIdP(a.data, Policy.load()).mint(a.email, now, ttl_s=a.minutes * 60))


def cmd_provision(a):
    from .metabase import provision
    provision(a.metabase, Policy.load())


def cmd_serve(a):
    from .serve import serve
    serve(Path(a.data), a.port, a.metabase)


def cmd_mcp(a):
    from .mcp_server import main
    main(Path(a.data))


def main(argv=None):
    p = argparse.ArgumentParser(prog="lensgate", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="generate the synthetic warehouse, dev IdP and Metabase state")
    s.add_argument("data")
    s.set_defaults(fn=cmd_seed)
    s = sub.add_parser("ddl", help="print the Postgres DDL for the warehouse (or, with --roles, the grants)")
    s.add_argument("--roles", action="store_true")
    s.set_defaults(fn=cmd_ddl)
    s = sub.add_parser("demo", help="replay the seeded morning through the broker and gate")
    s.add_argument("data")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("check-dir", help="grade every turn.json under a directory; exit 1 if any fails")
    s.add_argument("dir")
    s.add_argument("--data", required=True)
    s.set_defaults(fn=cmd_check_dir)
    s = sub.add_parser("counterexamples", help="write the six turns that must be refused")
    s.add_argument("out")
    s.add_argument("--data", required=True)
    s.set_defaults(fn=cmd_counterexamples)
    s = sub.add_parser("sync", help="compare directory groups to Metabase memberships (exit 1 on drift)")
    s.add_argument("data")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--metabase", help="live Metabase URL; default is the offline state")
    s.set_defaults(fn=cmd_sync)
    s = sub.add_parser("token", help="mint a dev-IdP token for a seeded person (demo only)")
    s.add_argument("data")
    s.add_argument("email")
    s.add_argument("--minutes", type=int, default=60)
    s.set_defaults(fn=cmd_token)
    s = sub.add_parser("provision", help="configure a live Metabase from policy.yaml")
    s.add_argument("--metabase", default="http://localhost:3000")
    s.set_defaults(fn=cmd_provision)
    s = sub.add_parser("serve", help="chat panel beside the dashboards")
    s.add_argument("data")
    s.add_argument("--port", type=int, default=8700)
    s.add_argument("--metabase", help="live Metabase URL; default is the offline backend")
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("mcp", help="gated write tools for Claude, over stdio")
    s.add_argument("data")
    s.set_defaults(fn=cmd_mcp)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
