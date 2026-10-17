from pathlib import Path

from lensgate.policy import Policy
from lensgate.seed import ddl, roles_sh

ROOT = Path(__file__).resolve().parents[1]


def test_schema_sql_matches_generator():
    assert (ROOT / "docker/initdb/01-schema.sql").read_text(encoding="utf-8") == ddl() + "\n"


def test_roles_script_matches_policy():
    # The warehouse grants are generated from policy.yaml, so they cannot drift from it.
    assert (ROOT / "docker/initdb/02-roles.sh").read_text(encoding="utf-8") == roles_sh(Policy.load())


def test_policy_roles_reference_real_connections_and_collections():
    p = Policy.load()
    assert set(p.roles) == {"store_lead", "regional_ops", "finance", "executive"}
    for r in p.roles.values():
        assert len(r.idp_groups) == 1


def test_no_secret_values_in_env_example():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if any(w in key for w in ("PASSWORD", "KEY", "TOKEN", "SECRET")):
            assert value == "", f"{key} has a value in .env.example"
