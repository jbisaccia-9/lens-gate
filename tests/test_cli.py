import json
import subprocess
import sys

import pytest

from lensgate.serve import assert_no_admin_credentials


def run(*args, cwd=None):
    return subprocess.run([sys.executable, "-m", "lensgate", *args], capture_output=True, text=True, cwd=cwd)


def test_check_dir_refuses_counterexamples_each_for_its_own_rule(world):
    r = run("check-dir", str(world["cx"]), "--data", str(world["data"]))
    assert r.returncode == 1
    assert r.stdout.count("(own rule)") == 6 and "COLLATERAL" not in r.stdout


def test_check_dir_passes_the_demo(world):
    r = run("check-dir", str(world["root"] / "run"), "--data", str(world["data"]))
    assert r.returncode == 0, r.stdout
    assert "5/5 passed" in r.stdout


def test_sync_reports_then_fixes_drift(fresh_data):
    r = run("sync", str(fresh_data))
    assert r.returncode == 1 and "jordan.pike" in r.stdout and "stale Finance" in r.stdout
    assert run("sync", str(fresh_data), "--apply").returncode == 0
    r = run("sync", str(fresh_data))
    assert r.returncode == 0 and "0 difference(s)" in r.stdout


def test_after_sync_the_l6_counterexample_passes(fresh_data, world, tmp_path):
    # L6 is about the world, not the turn: fix the drift and the same record clears.
    import shutil
    run("sync", str(fresh_data), "--apply")
    d = tmp_path / "l6"
    shutil.copytree(world["cx"] / "l6-stale-membership", d)
    r = run("check-dir", str(d), "--data", str(fresh_data))
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("var", ["LENSGATE_MB_ADMIN_PASSWORD", "METABASE_API_KEY"])
def test_serve_and_mcp_refuse_to_start_with_admin_credentials(var):
    with pytest.raises(SystemExit, match="refusing to start"):
        assert_no_admin_credentials({var: "anything"})
    assert_no_admin_credentials({"LENSGATE_DEMO_PASSWORD": "fine"})


def test_token_mints_for_seeded_person(world):
    r = run("token", str(world["data"]), "avery.lin@larkmoor.example")
    assert r.returncode == 0 and r.stdout.count(".") == 2
