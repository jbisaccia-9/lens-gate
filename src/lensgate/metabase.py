"""Live Metabase: a REST client, a backend with the same calls as LocalBackend,
and `provision`, which builds the whole instance from policy.yaml.

Configuration is by environment variable name only (see .env.example). Nothing
here is exercised against a running Metabase in CI - the contract test in
tests/test_provision_contract.py checks the calls it makes, and the README says
which version it was written against. Treat it as a starting point you verify
on your own instance, not a guarantee.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .backend import Credential
from .policy import Policy, load_dashboards

LIVE_STATE = Path(".lensgate") / "live.json"  # ids only, no secrets; gitignored


class MetabaseError(RuntimeError):
    pass


class Client:
    def __init__(self, url: str, session: str | None = None, transport=None):
        self.url = url.rstrip("/")
        self.session = session
        self._custom = transport
        self.transport = transport or self._urllib

    def _urllib(self, method: str, path: str, body):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.session:
            req.add_header("X-Metabase-Session", self.session)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise MetabaseError(f"{method} {path}: HTTP {e.code}: {e.read()[:300]!r}") from e
        return json.loads(raw) if raw else None

    def get(self, path):
        return self.transport("GET", path, None)

    def post(self, path, body=None):
        return self.transport("POST", path, body or {})

    def put(self, path, body):
        return self.transport("PUT", path, body)

    def delete(self, path):
        return self.transport("DELETE", path, None)

    def login(self, email: str, password: str) -> "Client":
        sid = self.post("/api/session", {"username": email, "password": password})["id"]
        return Client(self.url, sid, self._custom)


def _rows(x):
    return x["data"] if isinstance(x, dict) and "data" in x else x


# --------------------------------------------------------------------------
# The backend the broker uses when it talks to a real Metabase.
# --------------------------------------------------------------------------

class MetabaseBackend:
    """Same interface as LocalBackend. Holds one *person's* session, never an admin's."""

    def __init__(self, client: Client, policy: Policy, live: dict):
        self.c, self.policy, self.live = client, policy, live
        self.writes: list[dict] = []
        self._me = None

    def _current(self):
        if self._me is None:
            self._me = self.c.get("/api/user/current")
        return self._me

    def memberships(self, email: str) -> list[str]:
        me = self._current()
        if me.get("email") != email:
            raise MetabaseError("this backend holds a different person's session")
        by_id = {v: k for k, v in self.live["groups"].items()}
        ids = [m.get("id", m.get("group_id")) for m in me.get("user_group_memberships", [])]
        return sorted(by_id[i] for i in ids if i in by_id)

    def connections(self, cred: Credential) -> set[str]:
        return {d["name"] for d in _rows(self.c.get("/api/database")) if d["name"] in self.live["databases"]}

    def describe(self, cred: Credential) -> dict:
        out = {}
        for name in sorted(self.connections(cred)):
            meta = self.c.get(f"/api/database/{self.live['databases'][name]}/metadata")
            out[name] = {f"{t['schema']}.{t['name']}": [f["name"] for f in t.get("fields", [])] for t in meta.get("tables", [])}
        return out

    def run_native(self, cred: Credential, connection: str, sql: str) -> dict:
        r = self.c.post("/api/dataset", {"database": self.live["databases"][connection], "type": "native",
                                         "native": {"query": sql}})
        if r.get("status") != "completed":
            raise MetabaseError(r.get("error") or "query failed")
        return {"columns": [col["name"] for col in r["data"]["cols"]], "rows": r["data"]["rows"]}

    def save_question(self, cred, collection, name, connection, sql, display) -> dict:
        card = self.c.post("/api/card", {
            "name": name, "collection_id": self.live["collections"][collection], "display": display,
            "visualization_settings": {}, "dataset_query": {
                "type": "native", "native": {"query": sql}, "database": self.live["databases"][connection]}})
        self.live.setdefault("cards", {})[name] = card["id"]
        self.writes.append({"op": "save_question", "name": name, "collection": collection, "id": card["id"]})
        return {"id": card["id"]}

    def add_to_dashboard(self, cred, dashboard, card) -> dict:
        did = self.live["dashboards"][dashboard]
        d = self.c.get(f"/api/dashboard/{did}")
        cards = [{k: dc[k] for k in ("id", "card_id", "row", "col", "size_x", "size_y")} for dc in d.get("dashcards", [])]
        row = max((dc["row"] + dc["size_y"] for dc in cards), default=0)
        cards.append({"id": -1, "card_id": self.live["cards"][card], "row": row, "col": 0, "size_x": 12, "size_y": 6})
        self.c.put(f"/api/dashboard/{did}", {"dashcards": cards})
        self.writes.append({"op": "add_to_dashboard", "dashboard": dashboard, "card": card})
        return {"dashboard": dashboard}

    def dashboards(self, cred) -> list[str]:
        return sorted(self.live["dashboards"])

    def dashboard_collection(self, dashboard: str) -> str | None:
        return self.live.get("dashboard_collections", {}).get(dashboard)


# --------------------------------------------------------------------------
# Provisioning: policy.yaml -> a configured Metabase.
# --------------------------------------------------------------------------

def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"set {name} (see .env.example)")
    return v


def wait_healthy(c: Client, timeout_s: int = 300) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            if (c.get("/api/health") or {}).get("status") == "ok":
                return
        except Exception:  # noqa: BLE001 - not up yet
            pass
        time.sleep(3)
    raise MetabaseError("Metabase did not become healthy")


def provision(url: str, policy: Policy, transport=None, log=print, state_path: Path = LIVE_STATE) -> dict:
    c = Client(url, transport=transport)
    if transport is None:
        wait_healthy(c)
    admin_email, admin_pw = _env("LENSGATE_MB_ADMIN_EMAIL"), _env("LENSGATE_MB_ADMIN_PASSWORD")

    props = c.get("/api/session/properties")
    if not props.get("has-user-setup"):
        sid = c.post("/api/setup", {
            "token": props["setup-token"],
            "user": {"email": admin_email, "password": admin_pw, "first_name": "Lens", "last_name": "Admin",
                     "site_name": policy.company},
            "prefs": {"site_name": policy.company, "allow_tracking": False}})["id"]
        log("setup: first admin created, anonymous tracking off")
        c = Client(url, sid, transport)
    else:
        c = c.login(admin_email, admin_pw)

    live: dict = {"databases": {}, "groups": {}, "collections": {}, "dashboards": {}, "dashboard_collections": {}, "cards": {}}

    # 1. One Metabase database per connection, each with its own warehouse login.
    existing = {d["name"]: d["id"] for d in _rows(c.get("/api/database"))}
    for name in policy.connections:
        if name in existing:
            live["databases"][name] = existing[name]
            continue
        d = c.post("/api/database", {"engine": "postgres", "name": name, "is_full_sync": True, "details": {
            "host": os.environ.get("LENSGATE_PG_HOST", "warehouse"), "port": int(os.environ.get("LENSGATE_PG_PORT", "5432")),
            "dbname": os.environ.get("LENSGATE_PG_DB", "larkmoor"), "user": name,
            "password": _env(f"LENSGATE_PG_{name.upper()}_PASSWORD"), "ssl": False}})
        live["databases"][name] = d["id"]
    log(f"connections: {', '.join(policy.connections)}")

    # 2. Groups, one per role.
    groups = {g["name"]: g["id"] for g in c.get("/api/permissions/group")}
    for role in policy.roles.values():
        if role.metabase_group not in groups:
            groups[role.metabase_group] = c.post("/api/permissions/group", {"name": role.metabase_group})["id"]
        live["groups"][role.metabase_group] = groups[role.metabase_group]
    all_users = groups.get("All Users")

    # 3. Data permissions: a group may write SQL on exactly its role's connections.
    graph = c.get("/api/permissions/graph")
    blocked = "blocked" if os.environ.get("LENSGATE_MB_PRO") == "1" else "unrestricted"  # "blocked" needs a paid plan
    for role in policy.roles.values():
        gid = str(live["groups"][role.metabase_group])
        graph["groups"].setdefault(gid, {})
        for conn, dbid in live["databases"].items():
            ok = conn in role.connections
            graph["groups"][gid][str(dbid)] = {
                "view-data": "unrestricted" if ok else blocked,
                "create-queries": "query-builder-and-native" if ok else "no"}
    if all_users is not None:
        for dbid in live["databases"].values():
            graph["groups"].setdefault(str(all_users), {})[str(dbid)] = {"view-data": blocked, "create-queries": "no"}
    c.put("/api/permissions/graph", graph)
    log("data permissions: per-role connections; All Users can create no queries")

    # 4. Collections and who may read or curate them.
    existing = {col["name"]: col["id"] for col in _rows(c.get("/api/collection")) if isinstance(col.get("id"), int)}
    for path in policy.collections:
        leaf = path.split("/")[-1]
        parent = "/".join(path.split("/")[:-1])
        if leaf in existing and not parent:
            live["collections"][path] = existing[leaf]
            continue
        body = {"name": leaf}
        if parent:
            body["parent_id"] = live["collections"][parent]
        live["collections"][path] = c.post("/api/collection", body)["id"]
    cgraph = c.get("/api/collection/graph")
    for role in policy.roles.values():
        gid = str(live["groups"][role.metabase_group])
        cgraph["groups"].setdefault(gid, {})
        for path, cid in live["collections"].items():
            cgraph["groups"][gid][str(cid)] = "write" if path in role.curate else "read" if path in role.view else "none"
    if all_users is not None:
        cgraph["groups"].setdefault(str(all_users), {})["root"] = "none"
        for cid in live["collections"].values():
            cgraph["groups"][str(all_users)][str(cid)] = "none"
    c.put("/api/collection/graph", cgraph)
    log(f"collections: {', '.join(policy.collections)}")

    # 5. The static dashboards.
    for d in load_dashboards():
        dash = c.post("/api/dashboard", {"name": d["name"], "collection_id": live["collections"][d["collection"]]})
        dashcards = []
        for i, card in enumerate(d["cards"]):
            cid = c.post("/api/card", {
                "name": card["name"], "collection_id": live["collections"][d["collection"]], "display": card["display"],
                "visualization_settings": {}, "dataset_query": {"type": "native", "native": {"query": card["sql"]},
                                                                "database": live["databases"][card["connection"]]}})["id"]
            live["cards"][card["name"]] = cid
            dashcards.append({"id": -(i + 1), "card_id": cid, "row": (i // 2) * 6, "col": (i % 2) * 12,
                              "size_x": 12, "size_y": 6})
        c.put(f"/api/dashboard/{dash['id']}", {"dashcards": dashcards})
        live["dashboards"][d["name"]] = dash["id"]
        live["dashboard_collections"][d["name"]] = d["collection"]
    log(f"dashboards: {', '.join(live['dashboards'])}")

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(live, indent=2), encoding="utf-8")
    log(f"wrote {state_path} (ids only). Next: `python -m lensgate sync data --metabase {url} --apply` to create users and memberships.")
    return live


def sync_live(url: str, policy: Policy, directory: list[dict], apply: bool, transport=None, log=print,
              state_path: Path = LIVE_STATE) -> list[str]:
    """Directory -> Metabase users and group memberships. Reports drift; fixes it with apply=True."""
    live = json.loads(state_path.read_text(encoding="utf-8"))
    c = Client(url, transport=transport).login(_env("LENSGATE_MB_ADMIN_EMAIL"), _env("LENSGATE_MB_ADMIN_PASSWORD"))
    users = {u["email"]: u for u in _rows(c.get("/api/user"))}
    memberships = c.get("/api/permissions/membership")
    managed = {gid: name for name, gid in live["groups"].items()}
    drift = []
    for person in directory:
        want = {policy.roles[r].metabase_group for r in policy.roles_for_groups(person["groups"])}
        u = users.get(person["email"])
        if u is None:
            drift.append(f"{person['email']}: no Metabase user (wants {sorted(want)})")
            if not apply or not want:
                continue
            first, _, last = person["name"].partition(" ")
            u = c.post("/api/user", {"first_name": first, "last_name": last, "email": person["email"],
                                     "password": _env("LENSGATE_DEMO_PASSWORD")})
        have = {managed[m["group_id"]]: m["membership_id"] for m in memberships.get(str(u["id"]), [])
                if m["group_id"] in managed}
        for g in sorted(want - set(have)):
            drift.append(f"{person['email']}: missing {g}")
            if apply:
                c.post("/api/permissions/membership", {"group_id": live["groups"][g], "user_id": u["id"]})
        for g in sorted(set(have) - want):
            drift.append(f"{person['email']}: stale {g}")
            if apply:
                c.delete(f"/api/permissions/membership/{have[g]}")
    for line in drift:
        log(("fixed  " if apply else "drift  ") + line)
    return drift
