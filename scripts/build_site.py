"""Build docs/index.html - the GitHub Pages replay - from a real run. Run from the repo root.

Seeds a fresh world in a temp directory, asks every demo question as every
person in the directory through the real broker and gate (6 x 5 = 30 turns),
grades the six counterexamples, runs every dashboard card, and inlines all of
it as JSON into scripts/site_template.html. Nothing on the page is typed by
hand; tests/test_site.py rebuilds it and fails if docs/index.html differs.

Tokens, keys and turn ids are left out: they vary per run and the page
doesn't need them.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lensgate import counterexamples, demo, gate  # noqa: E402
from lensgate.backend import Credential, LocalBackend  # noqa: E402
from lensgate.broker import Broker  # noqa: E402
from lensgate.idp import DevIdP, Identity  # noqa: E402
from lensgate.policy import Policy, load_dashboards  # noqa: E402
from lensgate.providers import ScriptedProvider  # noqa: E402
from lensgate.seed import seed  # noqa: E402

TITLES = {
    "avery.lin@larkmoor.example": "Store lead",
    "marisol.tate@larkmoor.example": "Regional operations",
    "devin.okafor@larkmoor.example": "Finance",
    "priya.raman@larkmoor.example": "Executive",
    "jordan.pike@larkmoor.example": "Store lead, moved from Finance",
    "casey.wren@larkmoor.example": "Contractor",
}
QUESTIONS = [demo.Q_TREND, demo.Q_STOCKOUTS, demo.Q_MARGIN, demo.Q_LABOR, demo.Q_OPINC]
SVC = Credential("api-key", "svc-site-builder")


def card_payload(backend, name, c):
    res = backend.run_native(SVC, c["connection"], c["sql"])
    return {"name": name, "display": c["display"], "connection": c["connection"], "sql": c["sql"].strip(),
            "columns": res["columns"], "rows": res["rows"]}


def build() -> dict:
    policy = Policy.load()
    with tempfile.TemporaryDirectory() as tmp:
        data, out = Path(tmp) / "data", Path(tmp) / "out"
        seed(data)
        idp = DevIdP(data, policy)
        base = LocalBackend(data, policy)

        cards = {}
        dashboards = []
        for d in load_dashboards():
            dashboards.append({"name": d["name"], "collection": d["collection"], "cards": [c["name"] for c in d["cards"]]})
            for c in d["cards"]:
                cards[c["name"]] = card_payload(base, c["name"], c)

        people = []
        for email, person in idp.directory.items():
            groups = base.memberships(email)
            mb_roles = [r for r in (policy.role_for_metabase_group(g) for g in groups) if r]
            dir_roles = policy.roles_for_groups(person["groups"])
            people.append({
                "email": email, "name": person["name"], "title": TITLES[email],
                "directory_roles": dir_roles, "metabase_groups": groups,
                "connections": sorted(policy.connections_for(dir_roles)),
                "schemas": {c: sorted(policy.schemas_for(c)) for c in sorted(policy.connections_for(dir_roles))},
                "view": sorted(policy.view_for(mb_roles)),          # what Metabase would show them today
                "curate": sorted(policy.curate_for(dir_roles)),
                "drift": gate.sync_problems(
                    policy, Identity(email, person["name"], tuple(person["groups"]), tuple(dir_roles)), base)
                if dir_roles else [],
            })

        turns: dict[str, list] = {}
        for n, person in enumerate(people):
            email = person["email"]
            turns[email] = []
            for i, q in enumerate(QUESTIONS):
                backend = LocalBackend(data, policy)  # each turn starts from the provisioned state
                broker = Broker(policy, backend, idp.jwks(), ScriptedProvider())
                when = demo.at(f"{9 + i:02d}:{10 * n:02d}")
                r = broker.ask(idp.mint(email, when), q, when, out / email.split("@")[0])
                v = r["verdict"]
                calls = []
                for c in r["record"]["calls"]:
                    entry = {"tool": c["tool"], "refused": c.get("refused")}
                    if c["tool"] in ("run_sql", "save_question"):
                        entry.update(connection=c["connection"], sql=c["sql"])
                    if c["tool"] == "run_sql" and "result" in c:
                        entry["rows"] = len(c["result"]["rows"])
                    if c["tool"] == "save_question":
                        entry.update(name=c["name"], collection=c["collection"], display=c["display"])
                        if not c.get("refused") and c["name"] not in cards:
                            cards[c["name"]] = card_payload(base, c["name"], c)
                    if c["tool"] == "add_to_dashboard":
                        entry.update(dashboard=c["dashboard"], card=c["card"])
                    calls.append(entry)
                turns[email].append({
                    "outcome": r["outcome"], "answer": r["delivered"],
                    "checks": None if v is None else {k: (not p) for k, p in v.results.items()},
                    "skipped": [] if v is None else v.skipped,
                    "problems": {} if v is None else {k: p for k, p in v.results.items() if p},
                    "calls": calls,
                })

        cx_dir = Path(tmp) / "cx"
        cx = []
        for name in counterexamples.build(data, cx_dir):
            v = gate.grade_dir(cx_dir / name, base, policy, idp.jwks())
            cx.append({"name": name, "why": counterexamples.DESCRIPTIONS[name], "failed": v.failed,
                       "problems": [f"{r} {gate.NAMES[r]}: {p}" for r in v.failed for p in v.results[r]]})

    return {
        "company": policy.company,
        "questions": QUESTIONS,
        "people": people,
        "dashboards": dashboards,
        "cards": cards,
        "turns": turns,
        "counterexamples": cx,
        "rules": [{"id": r, "name": gate.NAMES[r]} for r in gate.RULES],
    }


def render(payload: dict) -> str:
    template = (ROOT / "scripts" / "site_template.html").read_text(encoding="utf-8")
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True).replace("</", "<\\/")
    return template.replace("/*__DATA__*/null", blob)


def main() -> None:
    html = render(build())
    out = ROOT / "docs" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    (ROOT / "docs" / ".nojekyll").write_text("", encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)} ({len(html) // 1024} KB)")


if __name__ == "__main__":
    main()
