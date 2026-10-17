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
    t.list_tables()
    t.run_sql("wh_sales", TREND_SQL)
    s = t.run_sql("wh_sales", TREND_SUMMARY_SQL)["rows"]
    t.save_question("Store Floor/Sandbox", "Weekly revenue by category, last 8 weeks", "wh_sales", TREND_SQL, "line")
    top, low = s[0], s[-1]
    mover = max(s, key=lambda r: abs(r[3]))
    return (f"{top[0]} led the last 8 weeks with {money(top[1])} in revenue ({pct(top[3])} vs the 8 weeks before). "
            f"{low[0]} was smallest at {money(low[1])} ({pct(low[3])}). The biggest swing was {mover[0]} at {pct(mover[3])}. "
            "I saved the weekly chart to Store Floor / Sandbox.")


Q_STOCKOUTS = "Which stores had the most stockout days in September?"
STOCKOUT_SQL = """SELECT st.store_name, st.region, SUM(i.stockout_days) AS stockout_days
FROM ops.inventory_weekly i JOIN ref.stores st ON st.store_id = i.store_id
WHERE i.week_start >= '2026-09-01' AND i.week_start < '2026-10-01'
GROUP BY st.store_name, st.region
ORDER BY stockout_days DESC, st.store_name
LIMIT 3"""


def plan_stockouts(t) -> str:
    t.list_tables()
    r = t.run_sql("wh_ops", STOCKOUT_SQL)["rows"]
    parts = [f"{name} ({region}) with {days}" for name, region, days in r]
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
    t.list_tables()
    r = t.run_sql("wh_finance", MARGIN_SQL)["rows"]
    t.save_question("Finance", "Q3 operating margin by region", "wh_finance", MARGIN_SQL, "bar")
    t.add_to_dashboard("Finance Monthly", "Q3 operating margin by region")
    lines = "; ".join(f"{reg} {m}% on {money(rev)} revenue" for reg, rev, _oi, m in r)
    return f"Q3 operating margin by region: {lines}. The chart is now on Finance Monthly."


Q_LABOR = ("Put a monthly revenue vs labor cost chart on the Executive Overview, and tell me how labor "
           "as a share of revenue moved from April to September.")
LABOR_SQL = """SELECT p.month, ROUND(SUM(p.revenue), 2) AS revenue, ROUND(SUM(p.labor_cost), 2) AS labor_cost,
       ROUND(100.0 * SUM(p.labor_cost) / SUM(p.revenue), 1) AS labor_pct_of_revenue
FROM finance.store_pnl_monthly p
GROUP BY p.month
ORDER BY p.month"""


def plan_labor(t) -> str:
    t.list_tables()
    r = t.run_sql("wh_exec", LABOR_SQL)["rows"]
    t.save_question("Executive", "Monthly revenue vs labor cost", "wh_exec", LABOR_SQL, "line")
    t.add_to_dashboard("Executive Overview", "Monthly revenue vs labor cost")
    first, last = r[0], r[-1]
    return (f"Labor ran {first[3]}% of revenue in {first[0]} and {last[3]}% in {last[0]} "
            f"(labor cost {money(first[2])} vs {money(last[2])}). The chart is on the Executive Overview.")


Q_OPINC = "What was operating income by store last quarter?"
OPINC_SQL = """SELECT st.store_name, ROUND(SUM(p.operating_income), 2) AS operating_income
FROM finance.store_pnl_monthly p JOIN ref.stores st ON st.store_id = p.store_id
WHERE p.fiscal_quarter = 'Q3'
GROUP BY st.store_name ORDER BY operating_income DESC"""


def plan_opinc(t) -> str:
    t.list_tables()
    r = t.run_sql("wh_finance", OPINC_SQL)
    if "refused" in r or "error" in r:
        return ("I can't show that: operating income is finance data, and your role doesn't include it. "
                "A finance partner can share it, or an admin can review your access.")
    return "Operating income by store: " + "; ".join(f"{n} {money(v)}" for n, v in r["rows"])


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
