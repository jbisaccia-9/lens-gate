import pytest

from lensgate import demo, sqlscope
from lensgate.policy import Policy, load_dashboards

SALES = {"ref", "sales"}


def test_every_shipped_query_is_in_scope_for_its_connection():
    policy = Policy.load()
    shipped = [(c["connection"], c["sql"]) for d in load_dashboards() for c in d["cards"]]
    shipped += [("wh_sales", demo.TREND_SQL), ("wh_sales", demo.TREND_SUMMARY_SQL), ("wh_ops", demo.STOCKOUT_SQL),
                ("wh_finance", demo.MARGIN_SQL), ("wh_exec", demo.LABOR_SQL), ("wh_finance", demo.OPINC_SQL)]
    for conn, sql in shipped:
        assert sqlscope.check(sql, policy.schemas_for(conn)) == [], (conn, sql)


def test_reads_tables_from_ctes_subqueries_and_comma_joins():
    sql = """WITH a AS (SELECT store_id FROM sales.weekly_sales), b (x) AS (SELECT 1 FROM ref.stores)
             SELECT * FROM a, ref.stores s JOIN (SELECT * FROM sales.weekly_sales) w ON w.store_id = s.store_id"""
    sc = sqlscope.scope(sql)
    assert sc.ok, sc.problems
    assert sc.tables == ["ref.stores", "sales.weekly_sales"]


@pytest.mark.parametrize("sql, fragment", [
    ("SELECT * FROM weekly_sales", "unqualified"),
    ("SELECT * FROM finance.store_pnl_monthly", "schema 'finance'"),
    ('SELECT * FROM "finance"."store_pnl_monthly"', "schema 'finance'"),
    ("SELECT * FROM sales.weekly_sales, finance.store_pnl_monthly", "schema 'finance'"),
    ("SELECT (SELECT SUM(operating_income) FROM finance.store_pnl_monthly) FROM sales.weekly_sales", "schema 'finance'"),
    ("SELECT store_id FROM sales.weekly_sales UNION SELECT store_id FROM finance.store_pnl_monthly", "schema 'finance'"),
    ("WITH weekly_sales AS (SELECT * FROM finance.store_pnl_monthly) SELECT * FROM weekly_sales", "schema 'finance'"),
    ("SELECT * FROM sales.weekly_sales w JOIN LATERAL (SELECT * FROM finance.store_pnl_monthly) p ON true", "schema 'finance'"),
    ("SELECT * FROM larkmoor.finance.store_pnl_monthly", "three-part"),
    ("SELECT 1; DROP TABLE sales.weekly_sales", "more than one statement"),
    ("SELECT 1 /* ; */ ; SELECT 2", "more than one statement"),
    ("DELETE FROM sales.weekly_sales", "not a SELECT"),
    ("SELECT * INTO sales.copy FROM sales.weekly_sales", "forbidden"),
    ("SELECT * FROM generate_series(1, 10)", "allowlist"),
    ("SELECT * FROM dblink('host=elsewhere', 'select 1') AS t(x int)", "allowlist"),
    # The hole probing found: SQL hidden in a string, run by the database itself.
    ("SELECT query_to_xml('select * from finance.store_pnl_monthly', true, false, '')", "query_to_xml"),
    ("SELECT pg_catalog.pg_read_file('/etc/hostname')", "schema-qualified function"),
    ("SELECT $q$x$q$", "dollar-quoted"),
])
def test_refuses(sql, fragment):
    problems = sqlscope.check(sql, SALES)
    assert problems, sql
    assert any(fragment in p for p in problems), problems


def test_from_inside_extract_and_substring_is_not_a_table():
    sql = "SELECT EXTRACT(YEAR FROM w.week_start), SUBSTRING(w.category FROM 1 FOR 3) FROM sales.weekly_sales w"
    assert sqlscope.check(sql, SALES) == []


def test_string_literals_are_not_tables():
    sql = "SELECT * FROM sales.weekly_sales WHERE category = 'from finance.store_pnl_monthly'"
    assert sqlscope.check(sql, SALES) == []
