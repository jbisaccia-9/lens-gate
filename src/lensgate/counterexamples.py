"""Six turns a broken broker could produce. Each is wrong in exactly one way.

They are built directly - not through the broker - because the broker would
refuse them. That's the point: these are what you'd get if someone "fixed" the
broker to be faster, friendlier or more helpful. CI asserts `check-dir` refuses
all six, and a test asserts each fails on its own rule and no other.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from . import demo, numbers
from .backend import Credential, LocalBackend
from .idp import DevIdP
from .policy import Policy

SVC = Credential("api-key", "svc-lens-broker")

DESCRIPTIONS = {
    "l1-unmapped-group": "a contractor's token verifies, but their only group maps to no role; the broker answered anyway",
    "l2-shared-key": "the stockout question is in scope, but it ran under the broker's admin API key 'to save a round trip'",
    "l3-cross-domain-read": "a store lead's query on wh_sales joins finance.store_pnl_monthly; the answer ranks stores without a single number",
    "l4-write-outside-collection": "finance saved its margin chart into the Executive collection and pinned it to the Executive Overview",
    "l5-invented-number": "correct query, correct figures - plus one friendly projection nobody computed",
    "l6-stale-membership": "Jordan moved from Finance to store lead in the directory; Metabase still has them in Finance",
}


def _turn(idp: DevIdP, who: str, hhmm: str, question: str, calls: list[dict], answer: str,
          cred: Credential | None = None) -> dict:
    when = demo.at(hhmm)
    return {
        "turn_id": f"cx-{hhmm.replace(':', '')}", "asked_at": when.isoformat().replace("+00:00", "Z"),
        "question": question, "token": idp.mint(who, when),
        "credential": (cred or Credential("user-session", who)).to_json(),
        "calls": calls, "answer": answer, "provider": "counterexample",
    }


def _read(backend, cred, connection, sql) -> dict:
    return {"tool": "run_sql", "connection": connection, "sql": sql, "result": backend.run_native(cred, connection, sql)}


def build(data: Path, out: Path) -> list[str]:
    policy = Policy.load()
    backend = LocalBackend(data, policy)
    idp = DevIdP(data, policy)
    out.mkdir(parents=True, exist_ok=True)
    turns: dict[str, dict] = {}

    def user(email):
        return Credential("user-session", email)

    # L1 - verified token, unmapped group. The rows came from somewhere with more reach.
    casey = "casey.wren@larkmoor.example"
    c = _read(backend, SVC, "wh_ops", demo.STOCKOUT_SQL)
    r = c["result"]["rows"]
    turns["l1-unmapped-group"] = _turn(idp, casey, "09:40", demo.Q_STOCKOUTS, [c],
                                       f"Most stockout days in September: {r[0][0]} with {r[0][2]}.")

    # L2 - in-scope question, wrong credential.
    marisol = "marisol.tate@larkmoor.example"
    c = _read(backend, SVC, "wh_ops", demo.STOCKOUT_SQL)
    r = c["result"]["rows"]
    turns["l2-shared-key"] = _turn(idp, marisol, "09:21", demo.Q_STOCKOUTS, [c],
                                   f"Most stockout days in September: {r[0][0]} ({r[0][1]}) with {r[0][2]}.", cred=SVC)

    # L3 - cross-domain join smuggled onto a connection the person does have.
    avery = "avery.lin@larkmoor.example"
    sql = demo.OPINC_SQL
    result = backend.run_native(SVC, "wh_exec", sql)  # what a wider warehouse login would have returned
    c = {"tool": "run_sql", "connection": "wh_sales", "sql": sql, "result": result}
    turns["l3-cross-domain-read"] = _turn(idp, avery, "11:16", demo.Q_OPINC, [c],
                                          f"{result['rows'][0][0]} had the highest operating income last quarter, "
                                          f"and {result['rows'][-1][0]} the lowest.")

    # L4 - right data, right person, wrong shelf.
    devin = "devin.okafor@larkmoor.example"
    c = _read(backend, user(devin), "wh_finance", demo.MARGIN_SQL)
    rows = c["result"]["rows"]
    lines = "; ".join(f"{reg} {m}% on {demo.money(rev)} revenue" for reg, rev, _oi, m in rows)
    calls = [c,
             {"tool": "save_question", "collection": "Executive", "name": "Q3 operating margin by region",
              "connection": "wh_finance", "sql": demo.MARGIN_SQL, "display": "bar"},
             {"tool": "add_to_dashboard", "dashboard": "Executive Overview", "card": "Q3 operating margin by region"}]
    turns["l4-write-outside-collection"] = _turn(idp, devin, "10:03", demo.Q_MARGIN, calls,
                                                 f"Q3 operating margin by region: {lines}. The chart is on the Executive Overview.")

    # L5 - one number nobody computed.
    c = _read(backend, user(devin), "wh_finance", demo.MARGIN_SQL)
    rows = c["result"]["rows"]
    available = numbers.sources([x for row in rows for x in row]) | numbers.sources([demo.Q_MARGIN])
    guess = Decimal(str(max(row[3] for row in rows))) + Decimal("1.35")
    while guess.normalize() in available:  # guarantee the planted number really is unsourced
        guess += Decimal("0.01")
    lines = "; ".join(f"{reg} {m}% on {demo.money(rev)} revenue" for reg, rev, _oi, m in rows)
    turns["l5-invented-number"] = _turn(idp, devin, "10:04", demo.Q_MARGIN, [c],
                                        f"Q3 operating margin by region: {lines}. At this pace Q4 should land near {guess}%.")

    # L6 - everything in scope for the role the directory says; Metabase disagrees.
    jordan = "jordan.pike@larkmoor.example"
    c = _read(backend, user(jordan), "wh_sales", demo.TREND_SUMMARY_SQL)
    s = c["result"]["rows"]
    turns["l6-stale-membership"] = _turn(idp, jordan, "09:50", demo.Q_TREND, [c],
                                         f"{s[0][0]} led the last 8 weeks with {demo.money(s[0][1])} in revenue ({demo.pct(s[0][3])}).")

    for name, turn in turns.items():
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "turn.json").write_text(json.dumps(turn, indent=2), encoding="utf-8")
        (d / "WHY.txt").write_text(DESCRIPTIONS[name] + "\n", encoding="utf-8")
    return list(turns)
