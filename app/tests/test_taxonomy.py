import ast
import os
from pathlib import Path

import pytest

os.environ["DATA_MODE"] = "demo"
os.environ["CHAT_DB"] = ":memory:"

from fastapi.testclient import TestClient

from backend.main import app
from backend.services import taxonomy

BLUESKY = Path(__file__).resolve().parents[2] / "bluesky"


def test_overview_and_trees_demo():
    with TestClient(app) as c:
        o = c.get("/api/taxonomies?hours=24").json()
        assert o["mode"] == "demo" and set(o["sizes"]) == set(taxonomy.KEYS)
        assert all(s["available"] and s["size"] > 0 for s in o["sizes"].values())
        assert o["window"]["with_topic"] <= o["window"]["embedded"] <= o["window"]["clean"]
        for key in taxonomy.KEYS:
            t = c.get(f"/api/taxonomies/{key}?hours=24").json()
            ids = {n["id"] for n in t["nodes"]}
            assert len(ids) == len(t["nodes"])                                   # unique ids
            assert all(n["parent"] is None or n["parent"] in ids for n in t["nodes"])
            assert all(n["posts"] >= 0 for n in t["nodes"]) and t["about"]["steps"]
            parents = {n["parent"] for n in t["nodes"]}
            assert all(n["id"] in parents for n in t["nodes"] if n["kind"] == "group")   # no empty groups
        iptc = c.get("/api/taxonomies/iptc?hours=24").json()
        assert len(iptc["about"]["anchors"]) == len(taxonomy.ANCHORS)
        assert {n["info"]["matched"] for n in iptc["nodes"]} == {True, False}    # level 0 is roll-up only
        small = c.get("/api/taxonomies/people?hours=1").json()
        big = c.get("/api/taxonomies/people?hours=168").json()
        assert sum(n["posts"] for n in small["nodes"]) < sum(n["posts"] for n in big["nodes"])
        assert c.get("/api/taxonomies/planets").status_code == 404
        assert c.get("/api/taxonomies?hours=0").status_code == 422


def test_company_tree_nests_sector_industry_company():
    rows = [("Q1", "Acme", "maker", "[\"Acme Corp\"]", 1, 50, "Industrials", "Machinery"),
            ("Q2", "Bolt", "maker", None, 0, 40, "Industrials", None)]
    nodes = {n["id"]: n for n in taxonomy.company_tree(rows, {"Q1": 3})}
    assert nodes["Q1"]["parent"] == "ind:Industrials/Machinery" and nodes["ind:Industrials/Machinery"]["parent"] == "sector:Industrials"
    assert nodes["Q2"]["parent"] == "sector:Industrials"                         # no industry: straight under the sector
    assert nodes["Q1"]["posts"] == 3 and nodes["Q2"]["posts"] == 0 and nodes["Q1"]["info"]["aliases"] == ["Acme Corp"]


def _constants(path: Path) -> dict:
    """Module-level constants of a source file, without importing it (it needs numpy, Spark and so on)."""
    out = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                continue
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = value
                elif isinstance(t, ast.Tuple) and isinstance(value, tuple):
                    out.update({n.id: v for n, v in zip(t.elts, value)})
    return out


@pytest.mark.skipif(not BLUESKY.exists(), reason="bluesky/ source not next to the app")
def test_rules_shown_match_the_enrichment_code():
    enrich = _constants(BLUESKY / "src" / "enrich_lib.py")
    assert taxonomy.ANCHORS == enrich["ANCHORS"]
    assert taxonomy.MATCH_LEVELS == enrich["MATCH_LEVELS"]
    assert taxonomy.TOPIC_MIN_CHARS == enrich["TOPIC_MIN_CHARS"]
    env = dict(line.split("=", 1) for line in (BLUESKY / "config.env").read_text().splitlines()
               if "=" in line and not line.lstrip().startswith("#"))
    assert taxonomy.TOPIC_MIN_SCORE == float(env.get("TOPIC_MIN_SCORE", enrich["TOPIC_MIN_SCORE"]))
    entity = _constants(BLUESKY / "src" / "entity_lib.py")
    assert (taxonomy.CONF_FULL, taxonomy.CONF_TAG, taxonomy.CONF_SINGLE) == (entity["CONF_FULL"], entity["CONF_TAG"], entity["CONF_SINGLE"])
    loader = _constants(BLUESKY / "src" / "entity_loader.py")
    assert taxonomy.MIN_SITELINKS == loader["PERSON_MIN_SITELINKS"] == loader["COMPANY_MIN_SITELINKS"]
