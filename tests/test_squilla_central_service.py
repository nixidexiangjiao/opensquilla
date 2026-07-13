"""Contract tests for the standalone central routing service (services/squilla_central)."""

from __future__ import annotations

import json
import sys
import types

from services.squilla_central.server import (
    TEXT_TIERS,
    TIER_ANCHOR_TEXTS,
    Central,
    EmbeddingsConfig,
    MySqlConfig,
    MySqlStore,
    flat_anchor_texts,
)


class FakeStore:
    """In-memory store double with the MySqlStore method surface, so the service
    logic is fully tested without a live MySQL server."""

    def __init__(self) -> None:
        self.decisions: dict[str, dict] = {}
        self.ratings: dict[str, str] = {}

    def insert_decision(self, record: dict) -> None:
        self.decisions[record["decisionId"]] = dict(record)

    def _summary(self, record: dict) -> dict:
        summary = {key: value for key, value in record.items() if key != "embedding"}
        summary["rating"] = self.ratings.get(record["decisionId"])
        return summary

    def get_decision(self, decision_id: str) -> dict | None:
        record = self.decisions.get(decision_id)
        return self._summary(record) if record else None

    def list_decisions(self, tenant_id, session_key, limit):
        rows = [
            r
            for r in self.decisions.values()
            if r["tenantId"] == tenant_id
            and (session_key is None or r["sessionKey"] == session_key)
        ]
        rows.sort(key=lambda r: r["tsMs"], reverse=True)
        return [self._summary(r) for r in rows[:limit]]

    def record_feedback(self, decision_id, rating, ts_ms) -> bool:
        if decision_id not in self.decisions:
            return False
        self.ratings[decision_id] = rating
        return True

    def stats(self, tenant_id):
        rows = [r for r in self.decisions.values() if r["tenantId"] == tenant_id]

        def counts(key):
            out: dict = {}
            for r in rows:
                out[r[key]] = out.get(r[key], 0) + 1
            return out

        ratings: dict = {}
        for did, rating in self.ratings.items():
            if did in self.decisions and self.decisions[did]["tenantId"] == tenant_id:
                ratings[rating] = ratings.get(rating, 0) + 1
        return {
            "tiers": counts("finalTier"),
            "bands": counts("band"),
            "profiles": counts("profile"),
            "ratings": ratings,
        }

TIER_AXIS = {
    "c0": [1.0, 0.0, 0.0, 0.0],
    "c1": [0.0, 1.0, 0.0, 0.0],
    "c2": [0.0, 0.0, 1.0, 0.0],
    "c3": [0.0, 0.0, 0.0, 1.0],
}


def _axis_for(text: str) -> list[float]:
    for tier in TEXT_TIERS:
        if text.startswith(f"{tier}:") or text in TIER_ANCHOR_TEXTS[tier]:
            return list(TIER_AXIS[tier])
    return [0.5, 0.5, 0.5, 0.5]


def embed_stub(_config, texts):
    return [_axis_for(text) for text in texts]


def embed_fail(_config, _texts):
    return None


def make_central(embed_fn=embed_stub, token=None) -> Central:
    return Central(
        store=FakeStore(),
        embeddings=EmbeddingsConfig(url="http://stub"),
        policy_version="test-v1",
        token=token,
        embed_fn=embed_fn,
        now_ms=lambda: 1000,
    )


def route_body(message: str) -> dict:
    return {
        "tenantId": "t1",
        "sessionKey": "s1",
        "profile": "squilla/auto",
        "message": message,
        "attachmentCount": 0,
    }


def test_routes_semantically_and_stores_no_plaintext():
    central = make_central()
    message = "c3:设计一个跨区域容灾架构-SECRET-PAYLOAD"
    status, body = central.handle("POST", "/v1/route", {}, route_body(message), None)
    assert status == 200
    assert body["tier"] == "c3"
    assert body["meta"]["band"] == "semantic"

    stored = central.store.get_decision(body["decisionId"])
    assert stored["baseTier"] == "c3"
    assert stored["finalTier"] == "c3"
    assert stored["profile"] == "squilla/auto"
    assert stored["charLen"] == len(message)
    assert stored["policyVersion"] == "test-v1"
    assert len(stored["topAnchors"]) > 0
    assert "SECRET-PAYLOAD" not in str(stored)


def test_heuristic_fallback_when_embeddings_fail():
    central = make_central(embed_fn=embed_fail)
    status, body = central.handle("POST", "/v1/route", {}, route_body("谢谢"), None)
    assert status == 200
    assert body["tier"] == "c0"
    assert body["meta"]["band"] == "short_plain"
    assert central.store.get_decision(body["decisionId"])["band"] == "short_plain"


def test_flag_upgrades_apply_centrally():
    central = make_central()
    status, body = central.handle(
        "POST", "/v1/route", {}, route_body("c0:把这个删除了直接部署到生产"), None
    )
    assert status == 200
    assert body["tier"] == "c2"
    assert body["meta"]["flagUpgraded"] is True


def test_validates_route_body():
    central = make_central()
    assert central.handle("POST", "/v1/route", {}, {"sessionKey": "s"}, None)[0] == 400
    assert (
        central.handle("POST", "/v1/route", {}, {"tenantId": "t", "message": "  "}, None)[0] == 400
    )


def test_trace_endpoints_feedback_and_stats():
    central = make_central()
    _, routed = central.handle("POST", "/v1/route", {}, route_body("c2:查一下这个报错"), None)
    decision_id = routed["decisionId"]

    status, detail = central.handle("GET", f"/v1/decisions/{decision_id}", {}, None, None)
    assert status == 200
    assert detail["finalTier"] == "c2"
    assert detail["rating"] is None

    status, _ = central.handle(
        "POST", "/v1/feedback", {}, {"decisionId": decision_id, "rating": "down"}, None
    )
    assert status == 200
    assert (
        central.handle("GET", f"/v1/decisions/{decision_id}", {}, None, None)[1]["rating"]
        == "down"
    )

    status, listed = central.handle(
        "GET", "/v1/decisions", {"tenantId": ["t1"], "sessionKey": ["s1"]}, None, None
    )
    assert status == 200
    assert len(listed["decisions"]) == 1

    status, stats = central.handle("GET", "/v1/stats", {"tenantId": ["t1"]}, None, None)
    assert stats["tiers"] == {"c2": 1}
    assert stats["profiles"] == {"squilla/auto": 1}
    assert stats["ratings"] == {"down": 1}


def test_unknown_decision_and_bad_feedback():
    central = make_central()
    assert central.handle("GET", "/v1/decisions/nope", {}, None, None)[0] == 404
    assert (
        central.handle(
            "POST", "/v1/feedback", {}, {"decisionId": "nope", "rating": "down"}, None
        )[0]
        == 404
    )
    assert (
        central.handle("POST", "/v1/feedback", {}, {"decisionId": "x", "rating": "meh"}, None)[0]
        == 400
    )


def test_bearer_token_required_on_v1_but_not_healthz():
    central = make_central(token="secret")
    assert central.handle("POST", "/v1/route", {}, route_body("hi"), None)[0] == 401
    assert central.handle("POST", "/v1/route", {}, route_body("c0:hi"), "Bearer secret")[0] == 200
    assert central.handle("GET", "/healthz", {}, None, None)[0] == 200


def test_anchor_texts_flatten_in_tier_order():
    texts = flat_anchor_texts()
    assert texts[0] == TIER_ANCHOR_TEXTS["c0"][0]
    assert texts[-1] == TIER_ANCHOR_TEXTS["c3"][-1]


# ---------------------------------------------------------------------------
# MySqlStore: injected fake DB-API driver (no live MySQL server needed). This
# exercises the store's real SQL dispatch — placeholder counts, param order,
# row->summary mapping, and the ON DUPLICATE KEY upsert dialect.
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.calls.append((sql, params))

    def fetchone(self):
        return self._conn.results.pop(0) if self._conn.results else None

    def fetchall(self):
        rows = self._conn.results.pop(0) if self._conn.results else []
        return rows


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list = []
        self.results: list = []  # preloaded fetch results, popped in order
        self.committed = 0

    def cursor(self):
        return _FakeCursor(self)

    def ping(self, reconnect=False):
        pass

    def commit(self):
        self.committed += 1

    def close(self):
        pass


def _fake_pymysql(conn: _FakeConn):
    module = types.ModuleType("pymysql")
    module.connect = lambda **kwargs: conn  # noqa: ARG005
    return module


def _make_store(monkeypatch, conn: _FakeConn) -> MySqlStore:
    monkeypatch.setitem(sys.modules, "pymysql", _fake_pymysql(conn))
    return MySqlStore(MySqlConfig(database="test"))


def _decision_record(decision_id="d-1", tenant="t1", final="c2", band="semantic"):
    return {
        "decisionId": decision_id,
        "tenantId": tenant,
        "sessionKey": "s1",
        "profile": "squilla/auto",
        "tsMs": 1000,
        "band": band,
        "baseTier": "c0",
        "gatedTier": "c0",
        "finalTier": final,
        "confidence": 0.9,
        "margin": 0.4,
        "probabilities": {"c0": 0.7, "c1": 0.1, "c2": 0.15, "c3": 0.05},
        "flags": {"highRisk": True},
        "charLen": 12,
        "attachmentCount": 0,
        "topAnchors": [{"text": "谢谢", "similarity": 0.8}],
        "policyVersion": "v1",
        "latencyMs": 5,
        "embedding": [0.1, 0.2],
    }


def test_mysqlstore_ddl_runs_on_construct(monkeypatch):
    conn = _FakeConn()
    _make_store(monkeypatch, conn)
    ddl = [sql for sql, _ in conn.calls if "CREATE TABLE" in sql]
    assert any("decisions" in sql and "INDEX idx_decisions_session" in sql for sql in ddl)
    assert any("feedback" in sql for sql in ddl)


def test_mysqlstore_insert_placeholder_and_param_order(monkeypatch):
    conn = _FakeConn()
    store = _make_store(monkeypatch, conn)
    conn.committed = 0  # ignore the DDL commit from construction
    store.insert_decision(_decision_record())
    insert = next(c for c in conn.calls if c[0].startswith("INSERT INTO decisions"))
    sql, params = insert
    assert sql.count("%s") == 19
    assert len(params) == 19
    assert params[0] == "d-1"  # decision_id first
    assert params[3] == "squilla/auto"  # profile after session_key
    assert json.loads(params[11]) == {"c0": 0.7, "c1": 0.1, "c2": 0.15, "c3": 0.05}
    assert json.loads(params[18]) == [0.1, 0.2]  # embedding last
    assert conn.committed == 1


def test_mysqlstore_get_maps_row_to_summary(monkeypatch):
    conn = _FakeConn()
    store = _make_store(monkeypatch, conn)
    # 18 summary columns in _SUMMARY_COLUMNS order, then the rating lookup row.
    row = (
        "d-1", "t1", "s1", "squilla/auto", 1000, "semantic", "c0", "c0", "c2", 0.9, 0.4,
        json.dumps({"c0": 0.7, "c1": 0.1, "c2": 0.15, "c3": 0.05}),
        json.dumps({"highRisk": True}), 12, 0,
        json.dumps([{"text": "谢谢", "similarity": 0.8}]), "v1", 5,
    )
    conn.results = [row, ("down",)]
    summary = store.get_decision("d-1")
    assert summary["decisionId"] == "d-1"
    assert summary["profile"] == "squilla/auto"
    assert summary["finalTier"] == "c2"
    assert summary["probabilities"]["c0"] == 0.7
    assert summary["flags"] == {"highRisk": True}
    assert summary["topAnchors"] == [{"text": "谢谢", "similarity": 0.8}]
    assert summary["rating"] == "down"


def test_mysqlstore_get_missing_returns_none(monkeypatch):
    conn = _FakeConn()
    store = _make_store(monkeypatch, conn)
    conn.results = [None]
    assert store.get_decision("nope") is None


def test_mysqlstore_feedback_upsert_dialect(monkeypatch):
    conn = _FakeConn()
    store = _make_store(monkeypatch, conn)
    conn.results = [(1,)]  # decision exists
    assert store.record_feedback("d-1", "down", 5) is True
    upsert = next(c for c in conn.calls if "INSERT INTO feedback" in c[0])
    assert "ON DUPLICATE KEY UPDATE" in upsert[0]
    assert upsert[1] == ("d-1", "down", 5)

    conn.calls.clear()
    conn.results = [None]  # decision missing
    assert store.record_feedback("nope", "down", 5) is False
    assert not any("INSERT INTO feedback" in c[0] for c in conn.calls)


def test_mysqlstore_stats_aggregates(monkeypatch):
    conn = _FakeConn()
    store = _make_store(monkeypatch, conn)
    conn.results = [
        [("c2", 3), ("c0", 1)],  # tiers
        [("semantic", 4)],  # bands
        [("squilla/auto", 4)],  # profiles
        [("down", 2)],  # ratings
    ]
    assert store.stats("t1") == {
        "tiers": {"c2": 3, "c0": 1},
        "bands": {"semantic": 4},
        "profiles": {"squilla/auto": 4},
        "ratings": {"down": 2},
    }
