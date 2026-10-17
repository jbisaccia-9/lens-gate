import json
import sqlite3

import pytest

from lensgate.backend import Credential, LocalBackend
from lensgate.policy import Policy, load_dashboards

ADMIN = Credential("api-key", "svc-lens-broker")


def test_connection_login_cannot_see_other_schemas_even_with_an_admin_key(ctx):
    # Layer 2 holds even when layer 1 (Metabase permissions) is bypassed by an admin key:
    # the wh_sales warehouse login simply has no finance schema.
    with pytest.raises(sqlite3.OperationalError):
        ctx["backend"].run_native(ADMIN, "wh_sales", "SELECT * FROM finance.store_pnl_monthly")


def test_metabase_layer_refuses_ungranted_connection(ctx):
    avery = Credential("user-session", "avery.lin@larkmoor.example")
    with pytest.raises(PermissionError):
        ctx["backend"].run_native(avery, "wh_finance", "SELECT 1")


def test_connections_are_read_only(ctx):
    with pytest.raises(sqlite3.OperationalError):
        ctx["backend"].run_native(ADMIN, "wh_sales", "DELETE FROM sales.weekly_sales")


def test_backend_refuses_writes_outside_curate(ctx):
    devin = Credential("user-session", "devin.okafor@larkmoor.example")
    with pytest.raises(PermissionError):
        ctx["backend"].save_question(devin, "Executive", "x", "wh_finance", "SELECT 1", "table")


def test_every_dashboard_card_runs_on_its_connection(ctx):
    for d in load_dashboards():
        for c in d["cards"]:
            r = ctx["backend"].run_native(ADMIN, c["connection"], c["sql"])
            assert r["rows"], (d["name"], c["name"])


def test_seed_is_deterministic(world, tmp_path):
    from lensgate.seed import generate
    assert generate() == generate()


def test_planted_drift_is_only_jordan(ctx):
    policy, b = ctx["policy"], ctx["backend"]
    for person in ctx["idp"].directory.values():
        want = {policy.roles[r].metabase_group for r in policy.roles_for_groups(person["groups"])}
        have = set(b.memberships(person["email"]))
        assert (want != have) == (person["email"] == "jordan.pike@larkmoor.example")
