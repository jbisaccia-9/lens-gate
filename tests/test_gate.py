import base64
import copy
import json
from datetime import timedelta

import pytest

from lensgate import demo, gate
from lensgate.idp import parse_time
from lensgate.seed import SEED  # noqa: F401  (documents that the world is fixed)
from conftest import load_turn

EXPECTED = {
    "l1-unmapped-group": "L1", "l2-shared-key": "L2", "l3-cross-domain-read": "L3",
    "l4-write-outside-collection": "L4", "l5-invented-number": "L5", "l6-stale-membership": "L6",
}


def grade(turn, ctx):
    return gate.grade(turn, ctx["backend"], ctx["policy"], ctx["jwks"])


def test_every_demo_turn_passes(world):
    assert len(world["demo"]) == 5
    for r in world["demo"]:
        assert r["verdict"].passed, (r["record"]["question"], r["verdict"].to_json())


def test_demo_outcomes(world):
    outcomes = [r["outcome"] for r in world["demo"]]
    assert outcomes[:4] == ["delivered"] * 4
    assert outcomes[4] == "delivered, 1 call(s) refused"


def test_out_of_scope_query_never_ran(world):
    finance_call = world["demo"][4]["record"]["calls"][0]
    assert finance_call["refused"].startswith("L3")
    assert "result" not in finance_call


def test_refusal_answer_has_no_numbers(world):
    from lensgate import numbers
    assert numbers.extract(world["demo"][4]["delivered"]) == []


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_counterexample_fails_its_own_rule_and_no_other(world, ctx, name):
    v = grade(load_turn(world["cx"] / name), ctx)
    assert not v.passed
    assert v.failed == [EXPECTED[name]], v.to_json()


def test_all_six_counterexamples_exist(world):
    assert sorted(world["cx_names"]) == sorted(EXPECTED)


# --- holes found by probing, kept as tests -------------------------------

def _good(world):
    return copy.deepcopy(world["demo"][2]["record"])  # finance margin turn: read, save, pin


def test_query_to_xml_smuggle_is_refused_by_l3(world, ctx):
    t = _good(world)
    t["calls"][1]["sql"] = "SELECT query_to_xml('select * from finance.store_pnl_monthly', true, false, '')"
    t["calls"][1]["connection"] = "wh_sales"
    assert "L3" in grade(t, ctx).failed


def test_tampered_result_is_refused_by_l5(world, ctx):
    t = _good(world)
    read = next(c for c in t["calls"] if c["tool"] == "run_sql" and "result" in c)
    read["result"]["rows"][0][3] = read["result"]["rows"][0][3] + 1
    assert grade(t, ctx).failed == ["L5"]


def test_invented_number_in_chart_title_is_refused_by_l5(world, ctx):
    t = _good(world)
    title = "Q3 margin by region (target 18%)"
    for c in t["calls"]:
        if c["tool"] == "save_question":
            c["name"] = title
        if c["tool"] == "add_to_dashboard":
            c["card"] = title
    v = grade(t, ctx)
    assert v.failed == ["L5"]
    assert any("chart title" in p for p in v.results["L5"])


def test_someone_elses_session_is_refused_by_l2(world, ctx):
    t = _good(world)
    t["credential"] = {"kind": "user-session", "principal": "priya.raman@larkmoor.example"}
    assert grade(t, ctx).failed == ["L2"]


def test_token_used_after_expiry_is_refused_by_l1(world, ctx):
    t = _good(world)
    t["asked_at"] = (parse_time(t["asked_at"]) + timedelta(hours=2)).isoformat()
    assert grade(t, ctx).failed == ["L1"]


def test_token_from_another_issuer_key_is_refused_by_l1(world, ctx):
    from cryptography.hazmat.primitives.asymmetric import rsa
    import jwt
    t = _good(world)
    claims = jwt.decode(t["token"], options={"verify_signature": False})
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    t["token"] = jwt.encode(claims, other, algorithm="RS256", headers={"kid": "dev-1"})
    assert grade(t, ctx).failed == ["L1"]


def test_groups_overage_fails_closed(world, ctx):
    from lensgate.idp import IdentityError, _identity_from_claims
    with pytest.raises(IdentityError, match="overage"):
        _identity_from_claims({"preferred_username": "x@larkmoor.example",
                               "_claim_names": {"groups": "src1"}}, ctx["policy"])


def test_extra_group_in_token_cannot_be_added_without_resigning(world, ctx):
    import jwt
    t = _good(world)
    head, body, sig = t["token"].split(".")
    claims = jwt.decode(t["token"], options={"verify_signature": False})
    claims["groups"].append("00000000-0000-4000-8000-0000000000a4")  # executive
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    t["token"] = ".".join([head, forged, sig])
    assert grade(t, ctx).failed == ["L1"]


def test_any_non_session_credential_is_refused_by_l2_not_just_listed_services(world, ctx):
    # Isolates the credential-kind check: this principal is on no deny list.
    t = _good(world)
    t["credential"] = {"kind": "api-key", "principal": "reporting-bot"}
    v = grade(t, ctx)
    assert v.failed == ["L2"]
    assert v.results["L2"] == ["work ran under credential kind 'api-key' (reporting-bot), not the person's own session"]


def test_pinning_to_another_teams_dashboard_is_refused_by_l4(world, ctx):
    # Isolates the dashboard check: the chart is saved where finance may save it,
    # and only the pin lands on the Executive Overview.
    t = _good(world)
    pin = next(c for c in t["calls"] if c["tool"] == "add_to_dashboard")
    pin["dashboard"] = "Executive Overview"
    v = grade(t, ctx)
    assert v.failed == ["L4"]
    assert len(v.results["L4"]) == 1 and "Executive Overview" in v.results["L4"][0]


def test_saving_into_another_teams_collection_is_refused_by_l4(world, ctx):
    t = _good(world)
    t["calls"] = [c for c in t["calls"] if c["tool"] != "add_to_dashboard"]
    next(c for c in t["calls"] if c["tool"] == "save_question")["collection"] = "Executive"
    v = grade(t, ctx)
    assert v.failed == ["L4"] and "collection 'Executive'" in v.results["L4"][0]


def test_missing_membership_is_drift_too(world, ctx):
    # The other direction of L6: the directory grants a role Metabase hasn't caught up to.
    ctx["backend"].state["users"]["devin.okafor@larkmoor.example"]["groups"] = []
    v = grade(_good(world), ctx)
    assert "L6" in v.failed
    assert any("that Metabase does not have yet" in p for p in v.results["L6"])
