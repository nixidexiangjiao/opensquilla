"""Contract tests for the standalone central routing service (services/squilla_central)."""

from __future__ import annotations

import json
import sys
import types

from services.squilla_central.server import (
    Central,
    MySqlConfig,
    MySqlStore,
    apply_sticky,
    snap_to_available,
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

    def last_tier(self, tenant_id, session_key):
        rows = [
            r
            for r in self.decisions.values()
            if r["tenantId"] == tenant_id and r["sessionKey"] == session_key
        ]
        rows.sort(key=lambda r: r["tsMs"], reverse=True)
        return rows[0]["finalTier"] if rows else None

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


class FakeClassifier:
    """Stand-in for V4Classifier (the real model needs the LFS bundle + ML deps,
    verified only on deploy). Returns a deterministic V4-shaped outcome so the
    central wiring — outcome -> trail -> generic wire response -> stats — is
    fully covered here. It ignores the message, which also proves no plaintext
    reaches the store."""

    def __init__(self, tier: str = "c2", route_class: str = "R2") -> None:
        self.tier = tier
        self.route_class = route_class

    def classify(self, message: str) -> dict:  # noqa: ARG002 - message intentionally unused
        return {
            "band": "v4",
            "base_tier": self.tier,
            "gated_tier": self.tier,
            "final_tier": self.tier,
            "confidence": 0.77,
            "margin": 0.3,
            "probabilities": {"c0": 0.05, "c1": 0.1, "c2": 0.6, "c3": 0.25},
            "flags": {"highRisk": True},
            "flag_upgraded": False,
            "top_anchors": [],
            "embedding": None,
            "route_class": self.route_class,
            "difficulty": 0.42,
        }


def make_central(classifier=None, token=None) -> Central:
    return Central(
        store=FakeStore(),
        classifier=FakeClassifier() if classifier is None else classifier,
        policy_version="test-v1",
        token=token,
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


def test_routes_via_v4_classifier_and_stores_no_plaintext():
    central = make_central(classifier=FakeClassifier(tier="c3", route_class="R3"))
    status, body = central.handle(
        "POST", "/v1/route", {}, route_body("设计一个跨区域容灾架构-SECRET-PAYLOAD"), None
    )
    assert status == 200
    assert body["tier"] == "c3"
    assert body["meta"]["band"] == "v4"
    assert body["meta"]["routeClass"] == "R3"
    assert body["meta"]["difficulty"] == 0.42

    stored = central.store.get_decision(body["decisionId"])
    assert stored["finalTier"] == "c3"
    assert stored["profile"] == "squilla/auto"
    assert stored["policyVersion"] == "test-v1"
    assert "SECRET-PAYLOAD" not in str(stored)


def test_heuristic_fallback_when_no_classifier():
    # classifier=None models the V4 bundle/deps being unavailable at startup.
    central = Central(
        store=FakeStore(), classifier=None, policy_version="test-v1", now_ms=lambda: 1
    )
    status, body = central.handle("POST", "/v1/route", {}, route_body("谢谢"), None)
    assert status == 200
    assert body["tier"] == "c0"
    assert body["meta"]["band"] == "short_plain"
    assert central.store.get_decision(body["decisionId"])["band"] == "short_plain"


def test_heuristic_flag_upgrade_when_no_classifier():
    central = Central(
        store=FakeStore(), classifier=None, policy_version="test-v1", now_ms=lambda: 1
    )
    status, body = central.handle(
        "POST", "/v1/route", {}, route_body("把这个删除了直接部署到生产"), None
    )
    assert status == 200
    assert body["tier"] == "c2"  # short_plain c0 -> highRisk flag upgrade -> c2
    assert body["meta"]["flagUpgraded"] is True


def test_healthz_reports_classifier():
    assert make_central().handle("GET", "/healthz", {}, None, None)[1]["classifier"] == "v4"
    assert (
        Central(store=FakeStore(), classifier=None).handle("GET", "/healthz", {}, None, None)[1][
            "classifier"
        ]
        == "heuristic"
    )


def test_snap_to_available_walks_up_then_down():
    assert snap_to_available("c2", ["c0", "c1", "c2", "c3"]) == "c2"
    assert snap_to_available("c2", ["c1", "c3"]) == "c3"  # up, never silent downgrade
    assert snap_to_available("c3", ["c0", "c1"]) == "c1"  # nothing above -> highest below
    assert snap_to_available("c1", []) == "c1"


def test_apply_sticky_blocks_only_short_turn_downgrades():
    cfg = {"enabled": True, "maxUserLen": 200}
    # short continuation + downgrade -> held on the warm tier
    assert apply_sticky("c0", "c2", 5, cfg) == ("c2", True)
    # long turn is not a continuation -> downgrade allowed
    assert apply_sticky("c0", "c2", 1_500, cfg) == ("c0", False)
    # upgrades always pass (a harder turn is worth the cache miss)
    assert apply_sticky("c3", "c0", 5, cfg) == ("c3", False)
    # no history / disabled -> no-op
    assert apply_sticky("c0", None, 5, cfg) == ("c0", False)
    assert apply_sticky("c0", "c2", 5, {"enabled": False}) == ("c0", False)


def test_route_applies_sticky_across_turns():
    central = make_central(classifier=FakeClassifier(tier="c2", route_class="R2"))
    first = central.handle("POST", "/v1/route", {}, route_body("排查这个报错"), None)[1]
    assert first["tier"] == "c2"

    # Same session, short follow-up that the classifier would put at c0:
    # sticky must hold it on c2 to keep the prompt cache warm.
    central.classifier = FakeClassifier(tier="c0", route_class="R0")
    body = {**route_body("继续"), "sessionKey": "s1"}
    status, second = central.handle("POST", "/v1/route", {}, body, None)
    assert status == 200
    assert second["tier"] == "c2"
    assert second["meta"]["stuck"] is True
    assert second["meta"]["classifierTier"] == "c0"
    # The trail records the SERVED tier, so it stays a truthful training label.
    assert central.store.get_decision(second["decisionId"])["finalTier"] == "c2"


def test_route_snaps_to_client_available_tiers():
    central = make_central(classifier=FakeClassifier(tier="c2", route_class="R2"))
    body = {**route_body("hi"), "availableTiers": ["c1", "c3"]}
    tier = central.handle("POST", "/v1/route", {}, body, None)[1]["tier"]
    assert tier == "c3"  # c2 unavailable -> up, never down to c1


def test_image_turn_bypasses_classifier_to_strongest_tier():
    central = make_central(classifier=FakeClassifier(tier="c0", route_class="R0"))
    body = {**route_body("看看这张图"), "hasImage": True, "availableTiers": ["c0", "c2"]}
    status, resp = central.handle("POST", "/v1/route", {}, body, None)
    assert status == 200
    assert resp["tier"] == "c2"
    assert resp["meta"]["band"] == "image"


def test_validates_route_body():
    central = make_central()
    assert central.handle("POST", "/v1/route", {}, {"sessionKey": "s"}, None)[0] == 400
    assert (
        central.handle("POST", "/v1/route", {}, {"tenantId": "t", "message": "  "}, None)[0] == 400
    )


def test_trace_endpoints_feedback_and_stats():
    central = make_central()  # FakeClassifier -> c2
    _, routed = central.handle("POST", "/v1/route", {}, route_body("查一下这个报错"), None)
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
    assert central.handle("POST", "/v1/route", {}, route_body("hi"), "Bearer secret")[0] == 200
    assert central.handle("GET", "/healthz", {}, None, None)[0] == 200


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
        "classifierTier": final,
        "stuck": False,
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
    assert sql.count("%s") == 21
    assert len(params) == 21
    assert params[0] == "d-1"  # decision_id first
    assert params[3] == "squilla/auto"  # profile after session_key
    assert json.loads(params[13]) == {"c0": 0.7, "c1": 0.1, "c2": 0.15, "c3": 0.05}
    assert json.loads(params[20]) == [0.1, 0.2]  # embedding last
    assert conn.committed == 1


def test_mysqlstore_get_maps_row_to_summary(monkeypatch):
    conn = _FakeConn()
    store = _make_store(monkeypatch, conn)
    # 20 summary columns in _SUMMARY_COLUMNS order, then the rating lookup row.
    row = (
        "d-1", "t1", "s1", "squilla/auto", 1000, "semantic", "c0", "c0", "c2",
        "c2", 0, 0.9, 0.4,
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

