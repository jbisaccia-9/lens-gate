"""The warehouse layer, on real Postgres: load the seed with the shipped init
scripts, then check (1) every shipped query returns exactly what the offline
backend returns, and (2) each connection's login can read its own schemas and
nothing else - including SQL smuggled inside a string.

Needs psql on PATH and PGHOST/PGPORT pointing at an empty Postgres where
$POSTGRES_USER can create a database. Passwords by env var name only.
Exits non-zero on any difference or any isolation surprise.
"""
import csv
import io
import os
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lensgate import demo
from lensgate.backend import Credential, LocalBackend
from lensgate.policy import Policy, load_dashboards

SU = os.environ.get("POSTGRES_USER", "lensgate")
DB = os.environ.get("POSTGRES_DB", "larkmoor")
failures = []


def psql(login, sql, db=DB, password=None, file=None):
    env = dict(os.environ)
    if password is not None:
        env["PGPASSWORD"] = password
    cmd = ["psql", "-U", login, "-d", db, "-v", "ON_ERROR_STOP=1", "--csv"] + (["-f", file] if file else ["-c", sql])
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def as_login(conn, sql):
    r = psql(conn, sql, password=os.environ[f"LENSGATE_PG_{conn.upper()}_PASSWORD"])
    if r.returncode:
        return None, (r.stderr.strip().splitlines() or ["error"])[0]
    return list(csv.reader(io.StringIO(r.stdout)))[1:], None


def norm(v):
    try:
        return Decimal(str(v)).normalize()
    except InvalidOperation:
        return str(v)


def load():
    su_pw = os.environ.get("PGPASSWORD")
    r = psql(SU, f"CREATE DATABASE {DB}", db="postgres", password=su_pw)
    if r.returncode:
        sys.exit(r.stderr)
    # Server-side COPY needs the files on the database host; \copy streams them from here.
    schema = Path("docker/initdb/01-schema.sql").read_text(encoding="utf-8")
    schema = re.sub(r"^COPY (\S+) FROM '/seed/csv/([^']+)' WITH \(FORMAT csv, HEADER true\);$",
                    lambda m: f"\\copy {m.group(1)} FROM 'data/warehouse/csv/{m.group(2)}' WITH (FORMAT csv, HEADER true)",
                    schema, flags=re.M)
    tmp = Path("out/pg_schema.sql")
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(schema, encoding="utf-8")
    r = psql(SU, None, password=su_pw, file=str(tmp))
    if r.returncode:
        sys.exit(r.stderr)
    r = subprocess.run(["sh", "docker/initdb/02-roles.sh"], capture_output=True, text=True,
                       env={**os.environ, "POSTGRES_USER": SU, "POSTGRES_DB": DB})
    if r.returncode:
        sys.exit(r.stderr)
    print("loaded the seed with 01-schema.sql and 02-roles.sh\n")


def main():
    load()
    policy = Policy.load()
    lb = LocalBackend("data", policy)
    svc = Credential("api-key", "svc")
    queries = [(c["connection"], f'{d["name"]} / {c["name"]}', c["sql"]) for d in load_dashboards() for c in d["cards"]]
    queries += [("wh_sales", "demo trend", demo.TREND_SQL), ("wh_sales", "demo trend summary", demo.TREND_SUMMARY_SQL),
                ("wh_ops", "demo stockouts", demo.STOCKOUT_SQL), ("wh_finance", "demo margin", demo.MARGIN_SQL),
                ("wh_exec", "demo labor", demo.LABOR_SQL), ("wh_finance", "demo operating income", demo.OPINC_SQL)]
    same = 0
    for conn, label, sql in queries:
        rows, err = as_login(conn, sql)
        if err:
            failures.append(label)
            print(f"FAIL  {label} on {conn}: {err}")
            continue
        local = lb.run_native(svc, conn, sql)["rows"]
        eq = [[norm(x) for x in r] for r in rows] == [[norm(x) for x in r] for r in local]
        same += eq
        if not eq:
            failures.append(label)
        print(("same  " if eq else "DIFF  ") + f"{label} on {conn} ({len(rows)} rows)")
    print(f"{same}/{len(queries)} identical on Postgres and the offline backend\n")

    expectations = [("wh_exec", "SELECT count(*) FROM finance.store_pnl_monthly", "allowed")]
    for conn, schemas in policy.connections.items():
        for other in sorted({s for v in policy.connections.values() for s in v} - set(schemas)):
            table = {"sales": "weekly_sales", "ops": "inventory_weekly", "people": "staff_hours_weekly",
                     "finance": "store_pnl_monthly"}[other]
            expectations.append((conn, f"SELECT count(*) FROM {other}.{table}", "refused"))
    expectations += [
        ("wh_sales", "SELECT query_to_xml('select * from finance.store_pnl_monthly', true, false, '')", "refused"),
        ("wh_sales", "DELETE FROM sales.weekly_sales", "refused"),
        ("wh_sales", "CREATE TABLE public.scratch (a int)", "refused"),
    ]
    for conn, sql, want in expectations:
        _, err = as_login(conn, sql)
        got = "refused" if err else "allowed"
        if got != want:
            failures.append(f"{conn}: {sql}")
        print(("ok    " if got == want else "FAIL  ") + f"{got:<8} {conn}: {sql[:70]}")
    print(f"\n{len(failures)} surprise(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
