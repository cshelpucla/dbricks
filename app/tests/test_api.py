import os

os.environ["DATA_MODE"] = "demo"
os.environ["CHAT_DB"] = ":memory:"

from fastapi.testclient import TestClient

from backend.main import app


def test_clusters_and_filters():
    with TestClient(app) as c:
        full = c.get("/api/clusters?hours=168").json()
        assert full["total"] > 1000 and full["clusters"]
        k = full["clusters"][0]
        assert {"id", "label", "topic", "n", "x", "y", "r", "trend", "langs"} <= set(k)
        assert -1 <= k["trend"] <= 1

        recent = c.get("/api/clusters?hours=1").json()
        assert recent["total"] < full["total"]

        ja = c.get("/api/clusters?hours=168&lang=ja").json()
        assert 0 < ja["total"] < full["total"]
        assert all(l[0] == "ja" for cl in ja["clusters"] for l in cl["langs"])

        none = c.get("/api/clusters?hours=168&q=zzzz-no-such-text").json()
        assert none["total"] == 0 and none["clusters"] == []


def test_meta_and_posts():
    with TestClient(app) as c:
        m = c.get("/api/meta?hours=24").json()
        assert m["mode"] == "demo" and m["topics"] and m["langs"]
        cid = c.get("/api/clusters?hours=24").json()["clusters"][0]["id"]
        posts = c.get(f"/api/clusters/{cid}/posts?hours=24&limit=5").json()["posts"]
        assert 0 < len(posts) <= 5 and "text" in posts[0]


def test_people_and_companies():
    with TestClient(app) as c:
        m = c.get("/api/meta?hours=168").json()
        assert m["people"] and m["companies"] and {"value", "label", "count"} <= set(m["people"][0])
        pid = m["people"][0]["value"]
        full = c.get("/api/clusters?hours=168").json()
        assert all({"people", "companies"} <= set(k) for k in full["clusters"])
        assert all(q in full["entities"] for k in full["clusters"] for q, _ in k["people"] + k["companies"])
        one = c.get(f"/api/clusters?hours=168&person={pid}").json()
        assert 0 < one["total"] <= m["people"][0]["count"] and one["total"] < full["total"]
        assert c.get(f"/api/clusters?hours=168&person={pid}&company={m['companies'][0]['value']}").json()["total"] <= one["total"]
        assert c.get("/api/clusters?hours=168&person=nobody").json()["total"] == 0


def test_bad_input_rejected():
    with TestClient(app) as c:
        assert c.get("/api/clusters?hours=0").status_code == 422
        assert c.get("/api/clusters?min_score=2").status_code == 422
        assert c.get("/api/clusters?lang=" + ",".join("x" * 3 for _ in range(60))).status_code == 422


def test_pipelines_and_costs_demo():
    with TestClient(app) as c:
        p = c.get("/api/pipelines?hours=24").json()
        assert [x["key"] for x in p["pipelines"]] == ["ingest", "silver", "enrich", "map"]
        assert p["pipelines"][0]["rows"] > 0 and p["pipelines"][2]["status"] == "not deployed"
        s = c.get("/api/pipelines/silver/stats?hours=24").json()
        assert s["series"] and {"table", "ts", "rows", "bytes"} <= set(s["series"][0])
        assert c.get("/api/pipelines/nope/stats").status_code == 404

        for group in ("product", "sku", "resource"):
            k = c.get(f"/api/costs?days=30&group={group}").json()
            assert k["available"] and len(k["daily"]) == 30 and k["kpis"]["total"] > 0
        assert c.get("/api/costs?group=bogus").status_code == 422
        assert "events_raw" in c.get("/api/model/status").json()["existing"]


def test_cost_shape_folds_small_groups():
    from datetime import date

    from backend.services.costs import shape
    rows = [(date.today().isoformat(), f"g{i}", 1.0, float(20 - i)) for i in range(12)]
    out = shape(rows, 1, "sku")
    assert len(out["series"]) == 8 and out["series"][-1] == "Other"
    assert abs(sum(out["daily"][0]["values"].values()) - sum(20 - i for i in range(12))) < 0.5


def test_control_requires_header_and_works_in_demo():
    with TestClient(app) as c:
        r = c.get("/api/control").json()
        assert {x["key"] for x in r["resources"]} == {"ingest", "silver", "enrich", "map"}
        assert c.post("/api/control/ingest/pause").status_code == 403                    # no CSRF header
        h = {"X-Requested-With": "bluesky-analytics", "X-Forwarded-Email": "tester@example.com"}
        assert c.post("/api/control/ingest/explode", headers=h).status_code == 422
        assert c.post("/api/control/nope/pause", headers=h).status_code == 404
        res = c.post("/api/control/ingest/pause", headers=h).json()
        assert res["actor"] == "tester@example.com" and res["results"][0]["ok"]
        assert {x["key"]: x["status"] for x in c.get("/api/control").json()["resources"]}["ingest"] == "paused"
        allres = c.post("/api/control/all/resume", headers=h).json()["results"]
        assert [x["key"] for x in allres] == ["ingest", "silver", "enrich", "map"]
        assert allres[2]["ok"] is False                                                  # enrich not deployed in demo
        after = {x["key"]: x["status"] for x in c.get("/api/control").json()["resources"]}
        assert after["ingest"] == "running" and after["map"] == "scheduled"


def test_geo_endpoint_demo():
    with TestClient(app) as c:
        place = c.get("/api/geo?hours=24&min_conf=0.4&level=place").json()
        assert place["points"] and {"name", "country", "lat", "lon", "n", "topic"} <= set(place["points"][0])
        assert place["points"] == sorted(place["points"], key=lambda p: -p["n"]) and place["located"] <= place["total"]
        country = c.get("/api/geo?hours=24&level=country").json()
        assert len(country["points"]) < len(place["points"]) and all(len(p["country"]) == 2 for p in country["points"])
        assert c.get("/api/geo?level=galaxy").status_code == 422 and c.get("/api/geo?min_conf=3").status_code == 422


def test_geo_person_and_company_filters():
    with TestClient(app) as c:
        full = c.get("/api/geo?hours=24").json()
        assert full["people"] and full["companies"] and {"value", "label", "count"} <= set(full["people"][0])
        qid = full["people"][0]["value"]
        one = c.get(f"/api/geo?hours=24&person={qid}").json()
        assert 0 < one["located"] < full["located"]
        assert one["people"] == full["people"]                  # options are not narrowed by the filter itself
        assert c.get("/api/geo?hours=24&company=no-such-id").json()["points"] == []


def _scripted(*steps):
    """A fake chat model: each step is a (tool_name, args) call or a final text string."""
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    it = iter(steps)

    def fn(messages, info):
        step = next(it)
        return ModelResponse(parts=[TextPart(step) if isinstance(step, str) else ToolCallPart(step[0], step[1])])
    return FunctionModel(fn)


def test_chat_query_pivot_charts_and_persistence():
    with TestClient(app) as c:
        c.app.state.chat_model = _scripted(
            ("query_data", {"hours": 168, "time_bucket": "1d", "filters": [{"column": "lang", "op": "in", "value": ["en", "ja"]}],
                            "group_by": ["bucket", "lang"], "aggs": [{"fn": "count", "as": "posts"}],
                            "pivot": {"index": "bucket", "on": "lang", "values": "posts"}, "sort_by": "bucket"}),
            ("make_chart", {"table": "t1", "type": "line", "x": "bucket", "y": ["en", "ja"], "title": "Posts per day"}),
            ("query_data", {"hours": 24, "group_by": ["topic"], "sort_by": "posts", "descending": True, "limit": 5}),
            ("make_chart", {"table": "t2", "library": "chartjs", "type": "bar", "x": "topic", "y": ["posts"], "title": "Top topics"}),
            "Done.")
        r = c.post("/api/chat", json={"message": "trend?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"] == "Done." and body["mode"] == "demo"
        assert [a["kind"] for a in body["artifacts"]] == ["table", "chart", "table", "chart"]
        t1 = body["artifacts"][0]
        assert t1["columns"] == ["bucket", "en", "ja"] and t1["total_rows"] >= 2
        assert body["artifacts"][1]["spec"]["data"][0]["type"] == "scatter" and len(body["artifacts"][1]["spec"]["data"]) == 2
        cj = body["artifacts"][3]["spec"]
        assert cj["type"] == "bar" and len(cj["data"]["labels"]) == 5

        # saved, listed, reopened
        cid = body["conversation_id"]
        assert [x["id"] for x in c.get("/api/chat/conversations").json()["conversations"]] == [cid]
        saved = c.get(f"/api/chat/conversations/{cid}").json()["messages"]
        assert [m["role"] for m in saved] == ["user", "assistant"] and len(saved[1]["artifacts"]) == 4

        # follow-up reuses table t2 from the stored conversation
        c.app.state.chat_model = _scripted(
            ("make_chart", {"table": "t2", "type": "pie", "x": "topic", "y": ["posts"], "title": "Share"}), "Pie.")
        r2 = c.post("/api/chat", json={"message": "as a pie", "conversation_id": cid}).json()
        assert [a["kind"] for a in r2["artifacts"]] == ["chart"] and r2["artifacts"][0]["spec"]["data"][0]["type"] == "pie"
        assert len(c.get(f"/api/chat/conversations/{cid}").json()["messages"]) == 4

        # other users cannot see it; delete removes it
        assert c.get(f"/api/chat/conversations/{cid}", headers={"x-forwarded-email": "other@x"}).status_code == 404
        assert c.delete(f"/api/chat/conversations/{cid}").status_code == 204
        assert c.get(f"/api/chat/conversations/{cid}").status_code == 404


def test_chat_tool_errors_are_returned_to_the_model():
    import pytest
    from backend.services import analyst
    with TestClient(app) as c:
        s = analyst.Session(c.app.state.source)
        with pytest.raises(analyst.ToolError, match="unknown column"):
            s.query({"group_by": ["nope"]})
        with pytest.raises(analyst.ToolError, match="not valid"):
            s.query({"filters": [{"column": "lang", "op": ">", "value": "a"}], "select": ["lang"]})
        with pytest.raises(analyst.ToolError, match="unknown table"):
            s.chart({"table": "t9", "type": "bar", "x": "a", "y": ["b"]})
        with pytest.raises(analyst.ToolError, match="whole dataset"):
            s.query({})
        assert s.describe(24)["rows"] > 1000

        # through the agent: a bad call is retried by the model and the run still completes
        c.app.state.chat_model = _scripted(("query_data", {"group_by": ["nope"]}),
                                           ("query_data", {"group_by": ["lang"], "limit": 3}), "ok")
        r = c.post("/api/chat", json={"message": "langs"}).json()
        assert r["reply"] == "ok" and r["artifacts"][0]["total_rows"] == 3


def test_chat_validation():
    with TestClient(app) as c:
        assert c.post("/api/chat", json={"message": ""}).status_code == 422
        assert c.post("/api/chat", json={"message": "hi", "conversation_id": "x" * 32}).status_code == 422
        assert c.post("/api/chat", json={"message": "hi", "conversation_id": "a" * 32}).status_code == 404


def test_chat_sql_compiles_plans_with_bound_values():
    from backend.services import analyst, chatdata
    p = analyst.plan({"hours": 48, "filters": [{"column": "lang", "op": "in", "value": ["en", "x') OR 1=1 --"]},
                                                {"column": "bot_score", "op": "<", "value": "3"}],
                      "top": {"column": "topic", "n": 5}, "time_bucket": "15m", "group_by": ["bucket", "topic"],
                      "aggs": [{"fn": "count", "as": "posts per bucket"}, {"fn": "mean", "column": "topic_score"}],
                      "sort_by": "bucket", "limit": 500})
    sql, params = chatdata.compile_sql(p)
    assert "OR 1=1" not in sql and params["f0_1"] == ("STRING", "x') OR 1=1 --") and params["f1"] == ("BIGINT", 3)
    assert "post_map" not in sql and "LIMIT 5" in sql and "/ 900) * 900" in sql
    assert "count(*) AS `posts per bucket`" in sql and "avg(`topic_score`) AS `mean_topic_score`" in sql
    assert sql.endswith("ORDER BY `bucket` ASC NULLS LAST LIMIT 200") and params["cutoff"][0] == "TIMESTAMP"
    # cluster_id joins post_map; a pivot fetches the grouped rows and pivots them here
    sql, _ = chatdata.compile_sql(analyst.plan({"group_by": ["cluster_id", "lang"], "pivot": {"index": "cluster_id", "on": "lang", "values": "posts"}}))
    assert "LEFT JOIN" in sql and sql.endswith(f"LIMIT {chatdata.MAX_GROUPS + 1}")
    import pytest
    for bad in ({"group_by": ["lang"], "aggs": [{"fn": "count", "as": "a`b"}]}, {"group_by": ["lang"], "sort_by": "nope"},
                {"filters": [{"column": "bot_score", "op": ">", "value": "high"}], "select": ["lang"]},
                {"group_by": ["lang"], "aggs": [{"fn": "count", "as": "lang"}]}):
        with pytest.raises(analyst.ToolError):
            analyst.plan(bad)


def test_chat_top_and_inline_chart_in_one_call():
    with TestClient(app) as c:
        c.app.state.chat_model = _scripted(
            ("query_data", {"hours": 24, "top": {"column": "lang", "n": 3}, "time_bucket": "1h", "group_by": ["bucket", "lang"],
                            "sort_by": "bucket", "limit": 200,
                            "chart": {"type": "line", "x": "bucket", "y": ["posts"], "series": "lang", "title": "Posts per hour"}}),
            "Done.")
        body = c.post("/api/chat", json={"message": "top 3 languages per hour"}).json()
        table, chart = body["artifacts"]
        assert table["kind"] == "table" and len({r[1] for r in table["rows"]}) == 3
        assert chart["kind"] == "chart" and chart["table"] == "t1" and len(chart["spec"]["data"]) == 3

        # a bad inline chart keeps the table and tells the model how to fix it
        from backend.services import analyst
        s = analyst.Session(c.app.state.source)
        out = s.query({"group_by": ["lang"], "chart": {"type": "bar", "x": "nope", "y": ["posts"], "title": "x"}})
        assert out["table"] == "t1" and "make_chart" in out["chart"] and [a["kind"] for a in s.artifacts] == ["table"]


def test_chat_stream_sends_status_artifacts_then_done():
    import json
    with TestClient(app) as c:
        c.app.state.chat_model = _scripted(
            ("query_data", {"group_by": ["topic"], "sort_by": "posts", "descending": True, "limit": 5,
                            "chart": {"type": "bar", "x": "topic", "y": ["posts"], "title": "Top topics"}}), "Here.")
        with c.stream("POST", "/api/chat/stream", json={"message": "top topics"}, headers={"accept-encoding": "gzip"}) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            assert "content-encoding" not in r.headers  # gzip would hold every event back until the end
            events = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
        kinds = [e["type"] for e in events]
        assert kinds[0] == "status" and kinds[-1] == "done" and kinds.count("artifact") == 2
        assert kinds.index("artifact") < kinds.index("done") and events[-1]["reply"] == "Here."
        saved = c.get(f"/api/chat/conversations/{events[-1]['conversation_id']}").json()["messages"]
        assert [m["role"] for m in saved] == ["user", "assistant"] and len(saved[1]["artifacts"]) == 2
        assert c.post("/api/chat/stream", json={"message": "hi", "conversation_id": "b" * 32}).status_code == 404
        assert c.get("/api/meta?hours=24", headers={"accept-encoding": "gzip"}).headers.get("content-encoding") == "gzip"


def test_delta_chat_store_answers_from_memory_and_writes_in_background(monkeypatch):
    import threading
    from backend.services import chatstore
    statements, gate = [], threading.Event()

    def fake_run(sql, params=None):
        if sql.startswith("INSERT"):
            gate.wait(5)  # a slow Delta commit
        statements.append(sql.split()[0])
        return [["c" * 32, "t0", "t1", "2", "hello"]] if sql.startswith("SELECT conversation_id") else []
    monkeypatch.setattr(chatstore.sqlexec, "run", fake_run)
    store = chatstore.DeltaChatStore()
    store.save_turn("u", "c" * 32, "hello", "hi there", [{"kind": "table"}])  # returns while the INSERT waits
    assert [m["content"] for m in store.get("u", "c" * 32)] == ["hello", "hi there"]
    assert store.get("u", "c" * 32)[1]["artifacts"] == [{"kind": "table"}] and "SELECT" not in statements
    gate.set()
    assert store.list("u")[0]["title"] == "hello"  # list waits for queued writes
    assert statements == ["CREATE", "INSERT", "SELECT"]
    store.delete("u", "c" * 32)
    assert statements[-1] == "DELETE" and store.get("u", "c" * 32) == []
