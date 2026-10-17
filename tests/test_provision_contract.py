"""Contract test: what `provision` asks Metabase to do, without a Metabase.

This checks the calls against the policy - every group gets native queries on
exactly its connections, All Users gets none, curate means write, the setup
turns tracking off, and the ids file holds no secrets. It cannot tell you the
calls are accepted by your Metabase version; run the stack for that.
"""
import json

import pytest

from lensgate.metabase import provision
from lensgate.policy import Policy, load_dashboards


class FakeMetabase:
    def __init__(self):
        self.calls, self.next_id = [], 100
        self.groups = [{"id": 1, "name": "All Users"}, {"id": 2, "name": "Administrators"}]
        self.graph = {"revision": 1, "groups": {}}
        self.cgraph = {"revision": 1, "groups": {}}
        self.dashcards = {}

    def _id(self):
        self.next_id += 1
        return self.next_id

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if (method, path) == ("GET", "/api/health"):
            return {"status": "ok"}
        if (method, path) == ("GET", "/api/session/properties"):
            return {"setup-token": "setup-x", "has-user-setup": False}
        if (method, path) == ("POST", "/api/setup"):
            return {"id": "session-x"}
        if (method, path) == ("GET", "/api/database"):
            return {"data": []}
        if (method, path) == ("GET", "/api/permissions/group"):
            return list(self.groups)
        if (method, path) == ("POST", "/api/permissions/group"):
            g = {"id": self._id(), "name": body["name"]}
            self.groups.append(g)
            return g
        if (method, path) == ("GET", "/api/permissions/graph"):
            return json.loads(json.dumps(self.graph))
        if (method, path) == ("PUT", "/api/permissions/graph"):
            self.graph = body
            return body
        if (method, path) == ("GET", "/api/collection"):
            return [{"id": "root", "name": "Our analytics"}]
        if (method, path) == ("GET", "/api/collection/graph"):
            return json.loads(json.dumps(self.cgraph))
        if (method, path) == ("PUT", "/api/collection/graph"):
            self.cgraph = body
            return body
        if method == "POST" and path in ("/api/database", "/api/collection", "/api/card", "/api/dashboard"):
            return {"id": self._id()}
        if method == "PUT" and path.startswith("/api/dashboard/"):
            self.dashcards[int(path.rsplit("/", 1)[1])] = body["dashcards"]
            return {}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def provisioned(monkeypatch, tmp_path):
    monkeypatch.setenv("LENSGATE_MB_ADMIN_EMAIL", "admin@larkmoor.example")
    monkeypatch.setenv("LENSGATE_MB_ADMIN_PASSWORD", "not-a-real-password")
    for conn in Policy.load().connections:
        monkeypatch.setenv(f"LENSGATE_PG_{conn.upper()}_PASSWORD", f"pw-{conn}")
    fake = FakeMetabase()
    live = provision("http://mb.invalid", Policy.load(), transport=fake, log=lambda *_: None,
                     state_path=tmp_path / "live.json")
    return fake, live, tmp_path / "live.json"


def test_setup_turns_tracking_off(provisioned):
    fake, _, _ = provisioned
    setup = next(b for m, p, b in fake.calls if p == "/api/setup")
    assert setup["prefs"]["allow_tracking"] is False


def test_each_connection_uses_its_own_warehouse_login(provisioned):
    fake, _, _ = provisioned
    dbs = [b for m, p, b in fake.calls if (m, p) == ("POST", "/api/database")]
    assert {d["name"]: d["details"]["user"] for d in dbs} == {c: c for c in Policy.load().connections}


def test_native_queries_exactly_on_role_connections(provisioned):
    fake, live, _ = provisioned
    policy = Policy.load()
    for role in policy.roles.values():
        gid = str(live["groups"][role.metabase_group])
        native = {conn for conn, dbid in live["databases"].items()
                  if fake.graph["groups"][gid][str(dbid)]["create-queries"] == "query-builder-and-native"}
        assert native == set(role.connections), role.name


def test_all_users_can_create_no_queries_and_sees_no_collection(provisioned):
    fake, live, _ = provisioned
    for dbid in live["databases"].values():
        assert fake.graph["groups"]["1"][str(dbid)]["create-queries"] == "no"
    assert set(fake.cgraph["groups"]["1"].values()) == {"none"}


def test_curate_means_write_and_nothing_else_does(provisioned):
    fake, live, _ = provisioned
    for role in Policy.load().roles.values():
        perms = fake.cgraph["groups"][str(live["groups"][role.metabase_group])]
        writable = {path for path, cid in live["collections"].items() if perms[str(cid)] == "write"}
        assert writable == set(role.curate), role.name


def test_dashboards_built_from_spec(provisioned):
    fake, live, _ = provisioned
    spec = {d["name"]: len(d["cards"]) for d in load_dashboards()}
    assert {name: len(fake.dashcards[did]) for name, did in live["dashboards"].items()} == spec


def test_ids_file_holds_no_secrets(provisioned):
    _, _, path = provisioned
    text = path.read_text(encoding="utf-8")
    assert "pw-" not in text and "not-a-real-password" not in text and "session-x" not in text
