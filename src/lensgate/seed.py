"""The synthetic world: a fictional 12-store retail chain, its warehouse, its
directory, and the Metabase state that provisioning would create.

Everything is generated from a fixed seed. Two properties are planted on
purpose and named here so nobody mistakes them for findings:

* Saltmarsh and Copper Ridge run short on Outdoor and Footwear in September
  (so the stockout question has a real answer).
* Jordan Pike moved from Finance to a store-lead role in the directory, but
  the Metabase "Finance" membership was never removed (so `sync` and L6 have
  real drift to find). This is the most common access bug in BI tools: the
  identity provider is right and the downstream tool is stale.
"""
from __future__ import annotations

import csv
import json
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .policy import Policy, load_dashboards

SEED = 1729

# (schema, table) -> [(column, postgres_type)]; SQLite gets TEXT/INTEGER/REAL.
TABLES: dict[tuple[str, str], list[tuple[str, str]]] = {
    ("ref", "stores"): [
        ("store_id", "INTEGER"), ("store_name", "TEXT"), ("region", "TEXT"),
        ("opened_on", "DATE"), ("square_feet", "INTEGER"),
    ],
    ("sales", "weekly_sales"): [
        ("week_start", "DATE"), ("store_id", "INTEGER"), ("category", "TEXT"),
        ("units", "INTEGER"), ("transactions", "INTEGER"), ("revenue", "NUMERIC(12,2)"),
    ],
    ("ops", "inventory_weekly"): [
        ("week_start", "DATE"), ("store_id", "INTEGER"), ("category", "TEXT"),
        ("on_hand_units", "INTEGER"), ("stockout_days", "INTEGER"),
    ],
    ("people", "staff_hours_weekly"): [
        ("week_start", "DATE"), ("store_id", "INTEGER"), ("job_family", "TEXT"),
        ("headcount", "INTEGER"), ("scheduled_hours", "NUMERIC(10,1)"), ("worked_hours", "NUMERIC(10,1)"),
    ],
    ("finance", "store_pnl_monthly"): [
        ("month", "TEXT"), ("fiscal_year", "INTEGER"), ("fiscal_quarter", "TEXT"), ("store_id", "INTEGER"),
        ("revenue", "NUMERIC(12,2)"), ("cogs", "NUMERIC(12,2)"), ("labor_cost", "NUMERIC(12,2)"),
        ("occupancy", "NUMERIC(12,2)"), ("other_opex", "NUMERIC(12,2)"), ("operating_income", "NUMERIC(12,2)"),
    ],
}

STORES = [
    ("Harbor Row", "Coastal"), ("Saltmarsh", "Coastal"), ("Lantern Hill", "Coastal"), ("Fernbank", "Coastal"),
    ("Millrace", "Valley"), ("Wrenfield", "Valley"), ("Juniper Flats", "Valley"), ("Bellwether", "Valley"),
    ("Copper Ridge", "Highlands"), ("Old Quarry", "Highlands"), ("Cedar Point", "Highlands"), ("Stonegate", "Highlands"),
]
CATEGORIES = {  # share of revenue, average ticket
    "Apparel": (0.30, 48.0), "Footwear": (0.18, 86.0), "Outdoor": (0.22, 64.0),
    "Home": (0.17, 39.0), "Accessories": (0.13, 22.0),
}
JOB_FAMILIES = {"Sales floor": 21.50, "Stockroom": 20.00, "Management": 34.00}  # hourly rate
FIRST_WEEK = date(2026, 4, 6)
N_WEEKS = 27  # through the week of 2026-10-05; October's books are not closed
CLOSED_MONTHS = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]

DOMAIN = "larkmoor.example"
PEOPLE = [  # email local part, display name, IdP group(s)
    ("avery.lin", "Avery Lin", ["00000000-0000-4000-8000-0000000000a1"]),
    ("marisol.tate", "Marisol Tate", ["00000000-0000-4000-8000-0000000000a2"]),
    ("devin.okafor", "Devin Okafor", ["00000000-0000-4000-8000-0000000000a3"]),
    ("priya.raman", "Priya Raman", ["00000000-0000-4000-8000-0000000000a4"]),
    ("jordan.pike", "Jordan Pike", ["00000000-0000-4000-8000-0000000000a1"]),
    ("casey.wren", "Casey Wren", ["00000000-0000-4000-8000-0000000000c9"]),  # contractors: unmapped
]
STALE_MEMBERSHIP = {"jordan.pike": ["Finance"]}  # planted drift, see module docstring


def _season(category: str, d: date) -> float:
    m = d.month + d.day / 31
    if category == "Outdoor":
        return 1.0 + 0.35 * max(0.0, 1 - abs(m - 7.5) / 2.0)
    if category == "Apparel":
        return 1.15 if d.month == 9 else 1.0
    if category == "Home":
        return 1.12 if d.month >= 10 else 1.0
    return 1.0


def generate() -> dict[tuple[str, str], list[tuple]]:
    rng = random.Random(SEED)
    rows: dict[tuple[str, str], list[tuple]] = {k: [] for k in TABLES}
    store_params = {}
    for i, (name, region) in enumerate(STORES, start=1):
        opened = date(2014 + rng.randrange(0, 10), rng.randrange(1, 13), 1)
        sqft = rng.randrange(9, 22) * 1000
        rows[("ref", "stores")].append((i, name, region, opened.isoformat(), sqft))
        store_params[i] = {
            "base": rng.uniform(38_000, 72_000), "cogs": rng.uniform(0.53, 0.58),
            "occupancy": round(rng.uniform(14_000, 26_000), 2), "name": name,
        }

    hours_by_month: dict[tuple[int, str], float] = {}
    revenue_by_month: dict[tuple[int, str], float] = {}
    for w in range(N_WEEKS):
        wk = FIRST_WEEK + timedelta(weeks=w)
        month = wk.strftime("%Y-%m")
        for sid, p in store_params.items():
            week_rev = 0.0
            for cat, (share, ticket) in CATEGORIES.items():
                rev = round(p["base"] * share * _season(cat, wk) * rng.uniform(0.94, 1.06), 2)
                units = max(1, int(rev / ticket * rng.uniform(0.95, 1.05)))
                tx = max(1, int(units / rng.uniform(1.4, 1.8)))
                rows[("sales", "weekly_sales")].append((wk.isoformat(), sid, cat, units, tx, rev))
                week_rev += rev
                stockouts = min(7, max(0, int(rng.gauss(0.6, 0.9))))
                if p["name"] in ("Saltmarsh", "Copper Ridge") and cat in ("Outdoor", "Footwear") and wk.month == 9:
                    stockouts = rng.randrange(3, 7)
                on_hand = int(units * rng.uniform(1.8, 3.2))
                rows[("ops", "inventory_weekly")].append((wk.isoformat(), sid, cat, on_hand, stockouts))
            revenue_by_month[(sid, month)] = revenue_by_month.get((sid, month), 0.0) + week_rev
            sched = {"Sales floor": week_rev / 320, "Stockroom": week_rev / 1100, "Management": 80.0}
            labor = 0.0
            for fam, rate in JOB_FAMILIES.items():
                s = round(sched[fam], 1)
                worked = round(s * rng.uniform(0.93, 1.04), 1)
                head = max(1, round(s / 32))
                rows[("people", "staff_hours_weekly")].append((wk.isoformat(), sid, fam, head, s, worked))
                labor += worked * rate * 1.18  # burdened
            hours_by_month[(sid, month)] = hours_by_month.get((sid, month), 0.0) + labor

    for month in CLOSED_MONTHS:
        q = f"Q{(int(month[5:]) - 1) // 3 + 1}"
        for sid, p in store_params.items():
            rev = round(revenue_by_month[(sid, month)], 2)
            cogs = round(rev * p["cogs"], 2)
            labor = round(hours_by_month[(sid, month)], 2)
            occ = p["occupancy"]
            other = round(rev * 0.06 + 2500, 2)
            oi = round(rev - cogs - labor - occ - other, 2)
            rows[("finance", "store_pnl_monthly")].append((month, 2026, q, sid, rev, cogs, labor, occ, other, oi))
    return rows


def ddl() -> str:
    """Postgres DDL for the warehouse. docker/initdb/01-schema.sql must equal this (a test checks)."""
    out = ["-- Generated by `python -m lensgate ddl`. Do not edit by hand.", ""]
    for schema in sorted({s for s, _ in TABLES}):
        out.append(f"CREATE SCHEMA IF NOT EXISTS {schema};")
    out.append("")
    for (schema, table), cols in TABLES.items():
        body = ",\n".join(f"  {c} {t}" for c, t in cols)
        out.append(f"CREATE TABLE {schema}.{table} (\n{body}\n);")
        out.append(f"COPY {schema}.{table} FROM '/seed/csv/{schema}.{table}.csv' WITH (FORMAT csv, HEADER true);")
        out.append("")
    return "\n".join(out)


def roles_sh(policy: Policy) -> str:
    """Postgres init script: one login per connection, granted only its schemas.

    docker/initdb/02-roles.sh must equal this (a test checks), so the warehouse
    grants can't drift from policy.yaml. Passwords come from env var names only.
    """
    lines = [
        "#!/bin/sh",
        "# Generated by `python -m lensgate ddl --roles`. Do not edit by hand.",
        "# One warehouse login per Metabase connection, each able to read only its own schemas.",
        "set -eu",
        'psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL',
        "REVOKE ALL ON SCHEMA public FROM PUBLIC;",
        'REVOKE CONNECT ON DATABASE "$POSTGRES_DB" FROM PUBLIC;',
    ]
    for conn, schemas in policy.connections.items():
        var = f"LENSGATE_PG_{conn.upper()}_PASSWORD"
        lines.append(f"CREATE ROLE {conn} LOGIN PASSWORD '${{{var}:?set {var}}}';")
        lines.append(f'GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO {conn};')
        for s in schemas:
            lines.append(f"GRANT USAGE ON SCHEMA {s} TO {conn};")
            lines.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO {conn};")
    lines += ["SQL", ""]
    return "\n".join(lines)


def _sqlite_type(pg: str) -> str:
    return "INTEGER" if pg == "INTEGER" else "REAL" if pg.startswith("NUMERIC") else "TEXT"


def write_warehouse(rows: dict, data: Path) -> None:
    wh = data / "warehouse"
    (wh / "csv").mkdir(parents=True, exist_ok=True)
    for schema in sorted({s for s, _ in TABLES}):
        db = wh / f"{schema}.sqlite"
        db.unlink(missing_ok=True)
        con = sqlite3.connect(db)
        for (s, table), cols in TABLES.items():
            if s != schema:
                continue
            con.execute(f"CREATE TABLE {table} ({', '.join(f'{c} {_sqlite_type(t)}' for c, t in cols)})")
            con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(cols))})", rows[(s, table)])
            with open(wh / "csv" / f"{s}.{table}.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow([c for c, _ in cols])
                w.writerows(rows[(s, table)])
        con.commit()
        con.close()


def write_idp(data: Path) -> None:
    """A dev identity provider shaped like Entra ID: RS256 tokens, a JWKS, a directory."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import jwt

    idp = data / "idp"
    idp.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (idp / "private.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "dev-1", "use": "sig", "alg": "RS256"})
    (idp / "jwks.json").write_text(json.dumps({"keys": [jwk]}, indent=2), encoding="utf-8")
    rng = random.Random(SEED + 1)
    directory = [
        {"email": f"{local}@{DOMAIN}", "name": name,
         "oid": f"{rng.getrandbits(32):08x}-0000-4000-8000-{rng.getrandbits(48):012x}", "groups": groups}
        for local, name, groups in PEOPLE
    ]
    (idp / "directory.json").write_text(json.dumps(directory, indent=2), encoding="utf-8")


def write_metabase_state(policy: Policy, data: Path) -> None:
    """What `provision` creates in Metabase, as the offline backend's state.

    Memberships are synced from the directory, except the planted stale one.
    """
    directory = json.loads((data / "idp" / "directory.json").read_text(encoding="utf-8"))
    users = {}
    for person in directory:
        roles = policy.roles_for_groups(person["groups"])
        groups = sorted(policy.roles[r].metabase_group for r in roles)
        groups += STALE_MEMBERSHIP.get(person["email"].split("@")[0], [])
        users[person["email"]] = {"name": person["name"], "groups": sorted(set(groups))}
    cards, dashboards = {}, {}
    for d in load_dashboards():
        dashboards[d["name"]] = {"collection": d["collection"], "cards": [c["name"] for c in d["cards"]]}
        for c in d["cards"]:
            cards[c["name"]] = {"collection": d["collection"], "connection": c["connection"],
                                "display": c["display"], "sql": c["sql"].strip()}
    state = {"users": users, "collections": policy.collections, "dashboards": dashboards, "cards": cards}
    mb = data / "metabase"
    mb.mkdir(parents=True, exist_ok=True)
    (mb / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def seed(data: str | Path) -> str:
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    policy = Policy.load()
    rows = generate()
    write_warehouse(rows, data)
    write_idp(data)
    write_metabase_state(policy, data)
    counts = ", ".join(f"{s}.{t} {len(r)}" for (s, t), r in rows.items())
    return (f"seeded {policy.company} (fictional): {len(STORES)} stores, {N_WEEKS} weeks, "
            f"{len(CLOSED_MONTHS)} closed months\n  rows: {counts}\n"
            f"  directory: {len(PEOPLE)} people; planted drift: jordan.pike still in Metabase 'Finance'")
