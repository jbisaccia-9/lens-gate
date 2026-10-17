"""Against a provisioned live stack: does Metabase itself enforce what policy.yaml says?

Run after `provision` and `sync --apply`. Exits non-zero on the first surprise.
Uses only the demo users' own sessions - never the admin login - for the checks.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from lensgate import demo
from lensgate.backend import Credential
from lensgate.broker import Broker
from lensgate.idp import DevIdP
from lensgate.metabase import LIVE_STATE, Client, MetabaseBackend, MetabaseError
from lensgate.policy import Policy
from lensgate.providers import ScriptedProvider

URL = os.environ.get("LENSGATE_METABASE_URL", "http://localhost:3000")
PW = os.environ["LENSGATE_DEMO_PASSWORD"]
policy = Policy.load()
live = json.loads(LIVE_STATE.read_text(encoding="utf-8"))
failures = []


def check(label, ok):
    print(("ok    " if ok else "FAIL  ") + label)
    if not ok:
        failures.append(label)


def as_user(email):
    return MetabaseBackend(Client(URL).login(email, PW), policy, dict(live)), Credential("user-session", email)


avery, cred = as_user("avery.lin@larkmoor.example")
check("store lead reads wh_sales", avery.run_native(cred, "wh_sales", "SELECT COUNT(*) FROM sales.weekly_sales")["rows"][0][0] > 0)
try:
    avery.run_native(cred, "wh_finance", "SELECT COUNT(*) FROM finance.store_pnl_monthly")
    check("Metabase refuses store lead on wh_finance", False)
except (MetabaseError, KeyError):
    check("Metabase refuses store lead on wh_finance", True)

exec_, ecred = as_user("priya.raman@larkmoor.example")
try:
    exec_.run_native(ecred, "wh_sales", "SELECT COUNT(*) FROM finance.store_pnl_monthly")
    check("warehouse login wh_sales cannot see finance, even for an executive", False)
except MetabaseError:
    check("warehouse login wh_sales cannot see finance, even for an executive", True)

check("L6 membership read from /api/user/current", avery.memberships("avery.lin@larkmoor.example") == ["Store Leads"])

# One real turn through the broker and gate, against live Metabase.
idp = DevIdP("data", policy)
now = datetime.now(timezone.utc)
r = Broker(policy, avery, idp.jwks(), ScriptedProvider()).ask(
    idp.mint("avery.lin@larkmoor.example", now), demo.Q_TREND, now, Path("out/live"), cred=cred)
check(f"live turn graded and delivered ({r['outcome']})", r["verdict"] is not None and r["verdict"].passed)

sys.exit(1 if failures else 0)
