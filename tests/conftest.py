import json
import shutil
from pathlib import Path

import pytest

from lensgate import counterexamples, demo
from lensgate.backend import LocalBackend
from lensgate.idp import DevIdP
from lensgate.policy import Policy
from lensgate.seed import seed


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("lensgate")
    data = root / "data"
    seed(data)
    demo_results = demo.run(data, root / "run")
    cx_names = counterexamples.build(data, root / "cx")
    return {"root": root, "data": data, "demo": demo_results, "cx": root / "cx", "cx_names": cx_names}


@pytest.fixture
def ctx(world):
    policy = Policy.load()
    return {
        "policy": policy,
        "backend": LocalBackend(world["data"], policy),
        "idp": DevIdP(world["data"], policy),
        "jwks": DevIdP(world["data"], policy).jwks(),
        "data": world["data"],
    }


@pytest.fixture
def fresh_data(world, tmp_path):
    """A private copy of the seeded data, for tests that change state."""
    d = tmp_path / "data"
    shutil.copytree(world["data"], d)
    return d


def load_turn(path: Path) -> dict:
    return json.loads((path / "turn.json").read_text(encoding="utf-8"))
