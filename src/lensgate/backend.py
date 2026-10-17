"""The offline stand-in for Metabase + the warehouse.

It enforces access the same two ways the live stack does, so the gate is
tested against something that pushes back:

1. **Metabase level** - a person may use a connection only if one of their
   Metabase groups grants it, and save only into collections their groups can
   curate. A service credential (an admin API key) can do anything, exactly
   like the real thing; that is why L2 exists.
2. **Warehouse level** - each connection is a fresh SQLite session with only
   that connection's schemas ATTACHed, read-only. A query that names
   `finance.*` on `wh_sales` doesn't fail a policy check here; the table simply
   does not exist for that login, which is how a Postgres role scoped with
   GRANT behaves.

The live equivalent is `metabase.MetabaseBackend`; both expose the same calls.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .policy import Policy


@dataclass(frozen=True)
class Credential:
    kind: str       # "user-session" | "api-key"
    principal: str  # the email the session belongs to, or the key's service principal

    def to_json(self) -> dict:
        return {"kind": self.kind, "principal": self.principal}


class LocalBackend:
    def __init__(self, data: str | Path, policy: Policy):
        self.data = Path(data)
        self.policy = policy
        self.state = json.loads((self.data / "metabase" / "state.json").read_text(encoding="utf-8"))
        self.writes: list[dict] = []

    # --- who -------------------------------------------------------------
    def memberships(self, email: str) -> list[str]:
        user = self.state["users"].get(email)
        return list(user["groups"]) if user else []

    def _roles(self, cred: Credential) -> list[str] | None:
        """None means unrestricted (a service credential)."""
        if cred.kind != "user-session":
            return None
        roles = [self.policy.role_for_metabase_group(g) for g in self.memberships(cred.principal)]
        return [r for r in roles if r]

    def connections(self, cred: Credential) -> set[str]:
        roles = self._roles(cred)
        return set(self.policy.connections) if roles is None else self.policy.connections_for(roles)

    def curate(self, cred: Credential) -> set[str]:
        roles = self._roles(cred)
        return set(self.policy.collections) if roles is None else self.policy.curate_for(roles)

    # --- read ------------------------------------------------------------
    def run_native(self, cred: Credential, connection: str, sql: str) -> dict:
        if connection not in self.connections(cred):
            raise PermissionError(f"{cred.principal} has no access to connection {connection}")
        con = sqlite3.connect(":memory:")
        try:
            for schema in sorted(self.policy.schemas_for(connection)):
                path = (self.data / "warehouse" / f"{schema}.sqlite").resolve().as_uri()
                con.execute(f"ATTACH DATABASE '{path}?mode=ro' AS {schema}")
            con.execute("PRAGMA query_only = ON")
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        finally:
            con.close()
        return {"columns": cols, "rows": rows}

    def describe(self, cred: Credential) -> dict[str, dict[str, list[str]]]:
        """connection -> schema.table -> columns, for what this credential can use."""
        from .seed import TABLES
        out: dict[str, dict[str, list[str]]] = {}
        for conn in sorted(self.connections(cred)):
            schemas = self.policy.schemas_for(conn)
            out[conn] = {f"{s}.{t}": [c for c, _ in cols] for (s, t), cols in TABLES.items() if s in schemas}
        return out

    # --- write -----------------------------------------------------------
    def save_question(self, cred: Credential, collection: str, name: str, connection: str,
                      sql: str, display: str) -> dict:
        if collection not in self.curate(cred):
            raise PermissionError(f"{cred.principal} cannot save into collection {collection}")
        if connection not in self.connections(cred):
            raise PermissionError(f"{cred.principal} has no access to connection {connection}")
        card = {"name": name, "collection": collection, "connection": connection, "display": display, "sql": sql}
        self.state["cards"][name] = card
        self.writes.append({"op": "save_question", **card, "by": cred.principal})
        return card

    def add_to_dashboard(self, cred: Credential, dashboard: str, card: str) -> dict:
        d = self.state["dashboards"].get(dashboard)
        if d is None:
            raise LookupError(f"no dashboard {dashboard}")
        if d["collection"] not in self.curate(cred):
            raise PermissionError(f"{cred.principal} cannot edit dashboard {dashboard}")
        if card not in self.state["cards"]:
            raise LookupError(f"no card {card}")
        d["cards"].append(card)
        self.writes.append({"op": "add_to_dashboard", "dashboard": dashboard, "card": card, "by": cred.principal})
        return {"dashboard": dashboard, "cards": list(d["cards"])}

    def dashboards(self, cred: Credential) -> list[str]:
        return sorted(self.state["dashboards"])

    def dashboard_collection(self, dashboard: str) -> str | None:
        d = self.state["dashboards"].get(dashboard)
        return d["collection"] if d else None
