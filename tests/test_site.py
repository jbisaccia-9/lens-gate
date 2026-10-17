"""The Pages replay must be what the code produces - never hand-edited, never stale."""
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _builder():
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_docs_index_matches_a_fresh_build():
    b = _builder()
    fresh = b.render(b.build())
    assert (ROOT / "docs" / "index.html").read_text(encoding="utf-8") == fresh, \
        "docs/index.html is stale: run `python scripts/build_site.py`"


def test_page_holds_no_tokens_or_secrets():
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"eyJ[A-Za-z0-9_-]{20,}\.", html), "a JWT made it into the page"
    assert "BEGIN" not in html and "private" not in html.lower()


def test_page_data_tells_the_same_story_as_the_gate():
    b = _builder()
    d = b.build()
    assert len(d["people"]) == 6 and all(len(t) == 5 for t in d["turns"].values())
    assert [c["failed"] for c in d["counterexamples"]] == [["L1"], ["L2"], ["L3"], ["L4"], ["L5"], ["L6"]]
    jordan = d["turns"]["jordan.pike@larkmoor.example"]
    assert all(t["outcome"] == "refused before running (L6)" for t in jordan)
    casey = d["turns"]["casey.wren@larkmoor.example"]
    assert all(t["outcome"] == "refused before running (L1)" for t in casey)
    # every delivered answer in the replay passed every check
    for turns in d["turns"].values():
        for t in turns:
            if t["outcome"].startswith("delivered"):
                assert t["checks"] and all(t["checks"].values()), t
