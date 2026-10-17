"""The seeded morning: five questions from four people, answered by the
scripted provider through the real broker and graded by the real gate.

The plans below are what a well-behaved model would do. They compute every
percentage in SQL and quote results verbatim, because that's the only way
an answer clears L5. The fifth question is a store lead asking for finance
data: the plan tries, the broker refuses the call before it runs, and the
answer says so without a single number.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

DAY = "2026-10-12"


def money(v) -> str:
    return f"{v:,.2f}"


def pct(v) -> str:
    return f"{v:+.1f}%"


# ---- plans ----------------------------------------------------------------
#
# Each plan reads with a connection the person actually holds (the narrowest
# one that covers the schemas it needs, as a sensible model would), and says
# so plainly when a read, save or pin is refused. Nothing claims a chart was
# saved unless the save returned ok.

def pick(t, schemas: set[str], default: str) -> str:
    """The narrowest granted connection whose schemas cover `schemas`; else `default` (which will be refused)."""
    granted = t.list_tables()["connections"]
    fits = [c for c in granted if schemas <= t.policy.schemas_for(c)]
    return min(fits, key=lambda c: (len(t.policy.schemas_for(c)), c)) if fits else default


def no_access(domain: str) -> str:
    return (f"I can't answer that: it needs {domain} data, and your role doesn't include it. "
            "If you think it should, ask an admin to review your access.")


def saved(r: dict, where: str) -> str:
    return f"I saved the chart to {where}." if r.get("ok") else f"I couldn't save the chart to {where}: your role can't edit it."


def pinned(r: dict, where: str) -> str:
    return f"It's now on {where}." if r.get("ok") else f"I couldn't add it to {where}: your role can't edit that dashboard."


Q_TREND = "How has weekly revenue by category trended over the last 8 weeks? Save it as a chart in my sandbox."
TREND_SQL = """SELECT w.week_start, w.category, ROUND(SUM(w.revenue), 2) AS revenue
FROM sales.weekly_sales w
WHERE w.week_start >= '2026-08-17'
GROUP BY w.week_start, w.category
ORDER BY w.week_start, w.category"""
TREND_SUMMARY_SQL = """WITH cur AS (
  SELECT category, SUM(revenue) AS rev FROM sales.weekly_sales WHERE week_start >= '2026-08-17' GROUP BY category
), prev AS (
  SELECT category, SUM(revenue) AS rev FROM sales.weekly_sales
  WHERE week_start >= '2026-06-22' AND week_start < '2026-08-17' GROUP BY category
)
SELECT cur.category, ROUND(cur.rev, 2) AS last_8_weeks, ROUND(prev.rev, 2) AS prior_8_weeks,
       ROUND(100.0 * (cur.rev - prev.rev) / prev.rev, 1) AS pct_change
FROM cur JOIN prev ON prev.category = cur.category
ORDER BY last_8_weeks DESC"""


def plan_trend(t) -> str:
    conn = pick(t, {"sales"}, "wh_sales")
    if "rows" not in t.run_sql(conn, TREND_SQL):
        return no_access("sales")
    s = t.run_sql(conn, TREND_SUMMARY_SQL)["rows"]
    r = t.save_question("Store Floor/Sandbox", "Weekly revenue by category, last 8 weeks", conn, TREND_SQL, "line")
    top, low = s[0], s[-1]
    mover = max(s, key=lambda r: abs(r[3]))
    return (f"{top[0]} led the last 8 weeks with {money(top[1])} in revenue ({pct(top[3])} vs the 8 weeks before). "
            f"{low[0]} was smallest at {money(low[1])} ({pct(low[3])}). The biggest swing was {mover[0]} at {pct(mover[3])}. "
            + saved(r, "Store Floor / Sandbox"))


Q_STOCKOUTS = "Which stores had the most stockout days in September?"
STOCKOUT_SQL = """SELECT st.store_name, st.region, SUM(i.stockout_days) AS stockout_days
FROM ops.inventory_weekly i JOIN ref.stores st ON st.store_id = i.store_id
WHERE i.week_start >= '2026-09-01' AND i.week_start < '2026-10-01'
GROUP BY st.store_name, st.region
ORDER BY stockout_days DESC, st.store_name
LIMIT 3"""


def plan_stockouts(t) -> str:
    r = t.run_sql(pick(t, {"ops"}, "wh_ops"), STOCKOUT_SQL)
    if "rows" not in r:
        return no_access("inventory")
    parts = [f"{name} ({region}) with {days}" for name, region, days in r["rows"]]
    return f"Most stockout days in September: {parts[0]}, then {parts[1]}, then {parts[2]}."


Q_MARGIN = "What was operating margin by region in Q3? Add it to the Finance Monthly dashboard."
MARGIN_SQL = """SELECT st.region, ROUND(SUM(p.revenue), 2) AS revenue,
       ROUND(SUM(p.operating_income), 2) AS operating_income,
       ROUND(100.0 * SUM(p.operating_income) / SUM(p.revenue), 1) AS operating_margin_pct
FROM finance.store_pnl_monthly p JOIN ref.stores st ON st.store_id = p.store_id
WHERE p.fiscal_year = 2026 AND p.fiscal_quarter = 'Q3'
GROUP BY st.region
ORDER BY operating_margin_pct DESC"""


def plan_margin(t) -> str:
    conn = pick(t, {"finance"}, "wh_finance")
    r = t.run_sql(conn, MARGIN_SQL)
    if "rows" not in r:
        return no_access("finance")
    s = t.save_question("Finance", "Q3 operating margin by region", conn, MARGIN_SQL, "bar")
    lines = "; ".join(f"{reg} {m}% on {money(rev)} revenue" for reg, rev, _oi, m in r["rows"])
    tail = pinned(t.add_to_dashboard("Finance Monthly", "Q3 operating margin by region"), "Finance Monthly") \
        if s.get("ok") else saved(s, "Finance")
    return f"Q3 operating margin by region: {lines}. {tail}"


Q_LABOR = ("Put a monthly revenue vs labor cost chart on the Executive Overview, and tell me how labor "
           "as a share of revenue moved from April to September.")
LABOR_SQL = """SELECT p.month, ROUND(SUM(p.revenue), 2) AS revenue, ROUND(SUM(p.labor_cost), 2) AS labor_cost,
       ROUND(100.0 * SUM(p.labor_cost) / SUM(p.revenue), 1) AS labor_pct_of_revenue
FROM finance.store_pnl_monthly p
GROUP BY p.month
ORDER BY p.month"""


def plan_labor(t) -> str:
    conn = pick(t, {"finance"}, "wh_exec")
    r = t.run_sql(conn, LABOR_SQL)
    if "rows" not in r:
        return no_access("finance")
    s = t.save_question("Executive", "Monthly revenue vs labor cost", conn, LABOR_SQL, "line")
    first, last = r["rows"][0], r["rows"][-1]
    tail = pinned(t.add_to_dashboard("Executive Overview", "Monthly revenue vs labor cost"), "the Executive Overview") \
        if s.get("ok") else saved(s, "Executive")
    return (f"Labor ran {first[3]}% of revenue in {first[0]} and {last[3]}% in {last[0]} "
            f"(labor cost {money(first[2])} vs {money(last[2])}). {tail}")


Q_OPINC = "What was operating income by store last quarter?"
OPINC_SQL = """SELECT st.store_name, ROUND(SUM(p.operating_income), 2) AS operating_income
FROM finance.store_pnl_monthly p JOIN ref.stores st ON st.store_id = p.store_id
WHERE p.fiscal_quarter = 'Q3'
GROUP BY st.store_name ORDER BY operating_income DESC"""


def plan_opinc(t) -> str:
    r = t.run_sql(pick(t, {"finance"}, "wh_finance"), OPINC_SQL)
    if "rows" not in r:
        return no_access("finance")
    return "Operating income by store: " + "; ".join(f"{n} {money(v)}" for n, v in r["rows"]) + "."


PLANS = {Q_TREND: plan_trend, Q_STOCKOUTS: plan_stockouts, Q_MARGIN: plan_margin, Q_LABOR: plan_labor, Q_OPINC: plan_opinc}

MORNING = [  # (who, when, question)
    ("avery.lin@larkmoor.example", "09:05", Q_TREND),
    ("marisol.tate@larkmoor.example", "09:20", Q_STOCKOUTS),
    ("devin.okafor@larkmoor.example", "10:02", Q_MARGIN),
    ("priya.raman@larkmoor.example", "10:30", Q_LABOR),
    ("avery.lin@larkmoor.example", "11:15", Q_OPINC),
]


def at(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hhmm}:00+00:00").astimezone(timezone.utc)


def run(data: Path, out: Path) -> list[dict]:
    import json
    from .backend import LocalBackend
    from .broker import Broker
    from .idp import DevIdP
    from .policy import Policy
    from .providers import ScriptedProvider

    policy = Policy.load()
    backend = LocalBackend(data, policy)
    idp = DevIdP(data, policy)
    broker = Broker(policy, backend, idp.jwks(), ScriptedProvider())
    results = []
    for who, hhmm, q in MORNING:
        results.append(broker.ask(idp.mint(who, at(hhmm)), q, at(hhmm), out))
    (out / "metabase_writes.json").write_text(json.dumps(backend.writes, indent=2), encoding="utf-8")
    return results
