"""Contract tests for the standalone central routing service (services/squilla_central)."""

from __future__ import annotations

import json
import sys
import types

import pytest

from services.squilla_central import server
from services.squilla_central.policy import (
    PolicyRule,
    PolicySnapshot,
    PolicySource,
    RuleAction,
    RuleScope,
)
from services.squilla_central.server import (
    Central,
    MySqlConfig,
    MySqlStore,
    apply_sticky,
    detect_complaint,
    snap_to_available,
)


def policy_with(*rules: PolicyRule) -> PolicySource:
    """A PolicySource pinned to a fixed rule set (no file, no reload thread)."""
    source = PolicySource()
    source.snapshot = PolicySnapshot(
        rules=list(rules), errors=[], source="test", loaded_at_ms=0, version="test"
    )
    return source


def rule(name: str, **action: object) -> PolicyRule:
    scope_keys = {"tenants", "profiles", "hours", "not_before_ms", "not_after_ms", "ratio"}
    meta_keys = {"priority", "enabled", "dry_run", "note"}
    scope = {k: v for k, v in action.items() if k in scope_keys}
    meta = {k: v for k, v in action.items() if k in meta_keys}
    act = {k: v for k, v in action.items() if k not in scope_keys and k not in meta_keys}
    return PolicyRule(
        name=name,
        action=RuleAction(**act),  # type: ignore[arg-type]
        scope=RuleScope(**scope),  # type: ignore[arg-type]
        **meta,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _complaint_terms(monkeypatch):
    """Pin the complaint table so tests do not depend on the optional
    opensquilla import (it pulls the engine's full dependency chain)."""
    monkeypatch.setattr(server, "_complaint_terms_cache", ("不对", "答非所问", "太差"))


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

    def last_decision(self, tenant_id, session_key):
        rows = [
            r
            for r in self.decisions.values()
            if r["tenantId"] == tenant_id and r["sessionKey"] == session_key
        ]
        rows.sort(key=lambda r: r["tsMs"], reverse=True)
        if not rows:
            return None
        return {
            "tier": rows[0]["finalTier"],
            "tainted": bool(rows[0]["tainted"]),
            "turnIndex": int(rows[0]["turnIndex"]),
        }

    def export_training_rows(self, tenant_id, since_ms, limit):
        rows = [
            r
            for r in self.decisions.values()
            if r["tenantId"] == tenant_id
            and not r["tainted"]
            and r["featuresB64"] is not None
            and r["tsMs"] >= since_ms
        ]
        rows.sort(key=lambda r: r["tsMs"])
        return [
            {"session_key": r["sessionKey"], "decision_id": r["decisionId"]}
            for r in rows[:limit]
        ]

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
            "training": {
                "decisions": len(rows),
                "withFeatures": sum(1 for r in rows if r["featuresB64"] is not None),
                "tainted": sum(1 for r in rows if r["tainted"]),
                "complaints": sum(1 for r in rows if r["complaint"]),
                "trainable": sum(
                    1 for r in rows if r["featuresB64"] is not None and not r["tainted"]
                ),
            },
        }


class FakeClassifier:
    """Stand-in for V4Classifier (the real model needs the LFS bundle + ML deps,
    verified only on deploy). Returns a deterministic V4-shaped outcome so the
    central wiring — outcome -> trail -> generic wire response -> stats — is
    fully covered here. It ignores the message, which also proves no plaintext
    reaches the store."""

    def __init__(
        self,
        tier: str = "c2",
        route_class: str = "R2",
        probabilities: dict | None = None,
    ) -> None:
        self.tier = tier
        self.route_class = route_class
        self.probabilities = probabilities or {"c0": 0.05, "c1": 0.1, "c2": 0.6, "c3": 0.25}

    def classify(self, message: str) -> dict:  # noqa: ARG002 - message intentionally unused
        return {
            "band": "v4",
            "base_tier": self.tier,
            "gated_tier": self.tier,
            "final_tier": self.tier,
            "confidence": 0.77,
            "margin": 0.3,
            "probabilities": dict(self.probabilities),
            "flags": {"highRisk": True},
            "flag_upgraded": False,
            "top_anchors": [],
            "embedding": None,
            "route_class": self.route_class,
            "difficulty": 0.42,
            # Mirrors V4Classifier's capture block (real vectors need the LFS
            # bundle; the shape is what the store and exporter depend on).
            "features_b64": "ZmFrZS1mZWF0dXJlcw==",
            "raw_bge_b64": None,
            "feature_schema_version": "fs-abc123",
        }


def make_central(classifier=None, token=None, rules=(), now_ms=None) -> Central:
    return Central(
        store=FakeStore(),
        classifier=FakeClassifier() if classifier is None else classifier,
        policy=policy_with(*rules),
        policy_version="test-v1",
        token=token,
        now_ms=now_ms or (lambda: 1000),
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


# --- Self-learning capture -------------------------------------------------


def test_detect_complaint_is_short_reactions_only():
    assert detect_complaint("不对") is True
    assert detect_complaint("答非所问") is True
    assert detect_complaint("帮我写个函数") is False
    # A long prompt that merely quotes a complaint term is not a complaint.
    assert detect_complaint("不对" + "x" * 300) is False


def test_complaint_terms_degrade_loudly_when_opensquilla_is_absent(monkeypatch, capsys):
    # The term table is an optional import; losing it silently would kill the
    # only correction signal in the corpus, so the failure must be announced.
    monkeypatch.setattr(server, "_complaint_terms_cache", None)
    monkeypatch.setitem(sys.modules, "opensquilla.engine.routing.policy_data", None)
    assert server.complaint_terms() == ()
    assert "complaint terms unavailable" in capsys.readouterr().out


def test_route_captures_the_fields_offline_training_consumes():
    central = make_central()
    _, routed = central.handle("POST", "/v1/route", {}, route_body("这个结果不对"), None)
    record = central.store.decisions[routed["decisionId"]]

    # The feature vector + its schema version are what a retrain actually eats;
    # without them the corpus is unusable no matter how many rows it has.
    assert record["featuresB64"] == "ZmFrZS1mZWF0dXJlcw=="
    assert record["featureSchemaVersion"] == "fs-abc123"
    # Label-alignment inputs.
    assert record["routeClass"] == "R2"
    assert record["complaint"] is True
    assert record["turnIndex"] == 0
    # Still no plaintext anywhere in the row.
    assert "不对" not in json.dumps(record, ensure_ascii=False)


def test_turn_index_increments_per_session():
    central = make_central()
    central.handle("POST", "/v1/route", {}, route_body("one"), None)
    ts = [2000]
    central.now_ms = lambda: ts[0]
    _, second = central.handle("POST", "/v1/route", {}, route_body("two"), None)
    assert central.store.decisions[second["decisionId"]]["turnIndex"] == 1


def test_export_omits_biased_turns():
    central = make_central(rules=[rule("peak-c3", weights={"c3": 10.0})])
    clean = make_central()
    # Same classifier, one service biased and one not: only the clean row is
    # exportable, which is the whole point of the taint flag.
    _, biased = central.handle("POST", "/v1/route", {}, route_body("hi"), None)
    _, plain = clean.handle("POST", "/v1/route", {}, route_body("hi"), None)

    assert central.handle("GET", "/v1/train/export", {"tenantId": ["t1"]}, None, None)[1] == {
        "samples": [],
        "count": 0,
    }
    exported = clean.handle("GET", "/v1/train/export", {"tenantId": ["t1"]}, None, None)[1]
    assert [s["decision_id"] for s in exported["samples"]] == [plain["decisionId"]]
    assert central.store.decisions[biased["decisionId"]]["tainted"] is True


def test_stats_reports_corpus_health():
    central = make_central(rules=[rule("up", weights={"c3": 10.0})])
    central.handle("POST", "/v1/route", {}, route_body("不对"), None)
    training = central.handle("GET", "/v1/stats", {"tenantId": ["t1"]}, None, None)[1]["training"]
    assert training == {
        "decisions": 1,
        "withFeatures": 1,
        "tainted": 1,
        "complaints": 1,
        "trainable": 0,
    }


# --- Operator control at route time ----------------------------------------


def route(central, **overrides) -> dict:
    body = {**route_body(overrides.pop("message", "hi")), **overrides}
    status, resp = central.handle("POST", "/v1/route", {}, body, None)
    assert status == 200
    return resp


def test_pin_forces_the_tier_regardless_of_the_model():
    # Classifier says c2; the pin must win, and survive snap.
    central = make_central(rules=[rule("freeze", floor="c0", ceiling="c0")])
    resp = route(central)
    assert resp["tier"] == "c0"
    assert resp["meta"]["classifierTier"] == "c2"
    assert resp["meta"]["baselineTier"] == "c2"
    assert resp["meta"]["tainted"] is True


def test_ceiling_caps_the_tier_and_floor_lifts_it():
    capped = route(make_central(rules=[rule("cap", ceiling="c1")]))
    assert capped["tier"] == "c1"

    lifted = route(
        make_central(
            classifier=FakeClassifier(tier="c0", route_class="R0"),
            rules=[rule("guarantee", floor="c2")],
        )
    )
    assert lifted["tier"] == "c2"


def test_ceiling_is_not_escaped_by_snap_or_sticky():
    # snap: the profile cannot serve the capped tier, so the candidate set —
    # not the raw tier list — is what snap picks from.
    central = make_central(rules=[rule("cap", ceiling="c1")])
    assert route(central, availableTiers=["c0", "c3"])["tier"] == "c0"

    # sticky: a previous c3 turn would normally be held on a short follow-up.
    # The ceiling must pull that hold down instead of preserving c3.
    central = make_central(classifier=FakeClassifier(tier="c3", route_class="R3"))
    assert route(central)["tier"] == "c3"
    central.policy = policy_with(rule("cap", ceiling="c1"))
    central.classifier = FakeClassifier(tier="c0", route_class="R0")
    central.now_ms = lambda: 2000
    assert route(central, message="继续")["tier"] == "c1"


def test_impossible_band_is_reported_and_does_not_break_routing():
    central = make_central(rules=[rule("cap", ceiling="c0")])
    resp = route(central, availableTiers=["c2", "c3"])
    assert resp["tier"] == "c2"  # nearest servable; routing still answers
    assert resp["meta"]["clampUnsatisfiable"] is True


def test_dry_run_records_the_intent_without_serving_it():
    central = make_central(rules=[rule("shadow", ceiling="c0", dry_run=True)])
    resp = route(central)
    assert resp["tier"] == "c2"  # the model's tier is still served
    assert resp["meta"]["biasMode"] == "dry_run"
    assert resp["meta"]["baselineTier"] == "c2"
    # Nothing was changed, so the row stays trainable.
    assert resp["meta"]["tainted"] is False
    assert central.store.decisions[resp["decisionId"]]["biasRule"] == "shadow"


def test_a_rule_that_changes_nothing_leaves_the_row_trainable():
    # Ceiling sits above the model's pick, so it binds nothing.
    central = make_central(rules=[rule("loose", ceiling="c3")])
    resp = route(central)
    assert resp["meta"]["biasMode"] == "applied"
    assert resp["meta"]["tainted"] is False


def test_taint_is_judged_on_the_served_tier_not_an_intermediate():
    # The weight shifts c2 -> c3, but the profile cannot serve c3 and snap
    # lands back on c2 — the same tier the model alone would have produced.
    central = make_central(rules=[rule("nudge", weights={"c3": 3.0})])
    resp = route(central, availableTiers=["c0", "c2"])
    assert resp["tier"] == "c2"
    assert resp["meta"]["baselineTier"] == "c2"
    assert resp["meta"]["tainted"] is False


def test_priority_decides_between_overlapping_rules():
    central = make_central(
        rules=[
            rule("broad", ceiling="c3", priority=0),
            rule("urgent", ceiling="c0", priority=50),
        ]
    )
    resp = route(central)
    assert resp["meta"]["biasRule"] == "urgent"
    assert resp["tier"] == "c0"


def test_expired_and_disabled_rules_are_inert():
    central = make_central(
        rules=[
            rule("expired", ceiling="c0", not_after_ms=500),
            rule("off", ceiling="c0", enabled=False),
        ],
        now_ms=lambda: 1000,
    )
    resp = route(central)
    assert resp["meta"]["biasRule"] == ""
    assert resp["tier"] == "c2"


def test_image_turns_are_never_touched_by_a_rule():
    central = make_central(rules=[rule("pin-cheap", floor="c0", ceiling="c0")])
    resp = route(central, message="看图", hasImage=True, availableTiers=["c0", "c2"])
    assert resp["tier"] == "c2"  # strongest available, vision wins
    assert resp["meta"]["biasRule"] == "" and resp["meta"]["tainted"] is False


def test_taint_propagates_through_sticky_to_the_next_turn():
    central = make_central(rules=[rule("boost", floor="c3")])
    first = route(central)
    assert first["tier"] == "c3" and first["meta"]["tainted"] is True

    # Rule gone, model says c0, but sticky holds the overridden c3 — still an
    # operator decision, so still excluded from training.
    central.policy = policy_with()
    central.classifier = FakeClassifier(tier="c0", route_class="R0")
    central.now_ms = lambda: 2000
    second = route(central, message="继续")
    assert second["tier"] == "c3" and second["meta"]["stuck"] is True
    assert second["meta"]["tainted"] is True

    # A later turn the model chooses freely is clean again.
    central.now_ms = lambda: 3000
    third = route(central, message="word " * 60)
    assert third["meta"]["stuck"] is False and third["meta"]["tainted"] is False


# --- Policy inspection endpoints -------------------------------------------


def test_policy_endpoint_shows_the_live_rule_set():
    central = make_central(rules=[rule("cap", ceiling="c1", note="cost control")])
    status, body = central.handle("GET", "/v1/policy", {}, None, None)
    assert status == 200
    assert body["ruleCount"] == 1
    assert body["rules"][0]["action"] == "ceiling=c1"
    assert body["rules"][0]["note"] == "cost control"


def test_simulate_answers_what_would_happen_without_routing():
    central = make_central(rules=[rule("cap", ceiling="c1")])
    status, body = central.handle(
        "POST",
        "/v1/policy/simulate",
        {},
        {"tenantId": "t1", "profile": "squilla/auto", "classifierTier": "c3"},
        None,
    )
    assert status == 200
    assert body["rule"]["name"] == "cap"
    assert (body["baselineTier"], body["proposedTier"], body["changed"]) == ("c3", "c1", True)
    # Nothing was recorded: simulation must not pollute the decision trail.
    assert central.store.decisions == {}

    assert central.handle("POST", "/v1/policy/simulate", {}, {}, None)[0] == 400


def test_simulate_shows_a_dry_run_rule_would_not_be_served():
    central = make_central(rules=[rule("shadow", floor="c0", ceiling="c0", dry_run=True)])
    body = central.handle(
        "POST", "/v1/policy/simulate", {}, {"tenantId": "t1", "classifierTier": "c2"}, None
    )[1]
    assert body["proposedTier"] == "c0"  # what it would do
    assert body["servedTier"] == "c2"  # what it actually does today
    assert body["changed"] is True


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
        "turnIndex": 3,
        "routeClass": "R2",
        "complaint": True,
        "featureSchemaVersion": "fs-abc123",
        "featuresB64": "ZmFrZQ==",
        "rawBgeB64": None,
        "biasRule": "peak",
        "biasMode": "applied",
        "baselineTier": "c1",
        "tainted": True,
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
    # The INSERT names its columns, so the placeholder count must match both the
    # column list and the params tuple — a drift between them would silently
    # shift values into the wrong columns on an ALTERed deployment.
    assert sql.count("%s") == len(params)
    assert sql.count("%s") == len(server._INSERT_COLUMNS.split(","))
    assert params[0] == "d-1"  # decision_id first
    assert params[3] == "squilla/auto"  # profile after session_key
    assert json.loads(params[13]) == {"c0": 0.7, "c1": 0.1, "c2": 0.15, "c3": 0.05}
    assert json.loads(params[20]) == [0.1, 0.2]  # embedding
    # Self-learning capture tail, in _INSERT_COLUMNS order.
    assert params[21:] == (
        3, "R2", 1, "fs-abc123", "ZmFrZQ==", None, "peak", "applied", "c1", 1,
    )
    assert conn.committed == 1


def test_mysqlstore_get_maps_row_to_summary(monkeypatch):
    conn = _FakeConn()
    store = _make_store(monkeypatch, conn)
    # Summary columns in _SUMMARY_COLUMNS order, then the rating lookup row.
    row = (
        "d-1", "t1", "s1", "squilla/auto", 1000, "semantic", "c0", "c0", "c2",
        "c2", 0, 0.9, 0.4,
        json.dumps({"c0": 0.7, "c1": 0.1, "c2": 0.15, "c3": 0.05}),
        json.dumps({"highRisk": True}), 12, 0,
        json.dumps([{"text": "谢谢", "similarity": 0.8}]), "v1", 5,
        3, "R2", 1, "peak", "applied", "c1", 1,
    )
    assert len(row) == len(server._SUMMARY_COLUMNS.split(","))
    conn.results = [row, ("down",)]
    summary = store.get_decision("d-1")
    assert summary["decisionId"] == "d-1"
    assert summary["profile"] == "squilla/auto"
    assert summary["finalTier"] == "c2"
    assert summary["probabilities"]["c0"] == 0.7
    assert summary["flags"] == {"highRisk": True}
    assert summary["topAnchors"] == [{"text": "谢谢", "similarity": 0.8}]
    assert summary["rating"] == "down"
    # The trail explains a surprising tier: which rule moved it, and whether
    # the row is excluded from training as a result.
    assert summary["turnIndex"] == 3
    assert summary["routeClass"] == "R2"
    assert summary["complaint"] is True
    assert summary["biasRule"] == "peak"
    assert summary["biasMode"] == "applied"
    assert summary["baselineTier"] == "c1"
    assert summary["tainted"] is True


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
        (4, 4, 1, 2, 3),  # corpus health: total, captured, tainted, complaints, trainable
        [("peak", "applied", 3, 2)],  # per-rule impact
    ]
    assert store.stats("t1") == {
        "tiers": {"c2": 3, "c0": 1},
        "bands": {"semantic": 4},
        "profiles": {"squilla/auto": 4},
        "ratings": {"down": 2},
        "training": {
            "decisions": 4,
            "withFeatures": 4,
            "tainted": 1,
            "complaints": 2,
            "trainable": 3,
        },
        "rules": [{"rule": "peak", "mode": "applied", "matched": 3, "changed": 2}],
    }


def test_mysqlstore_adds_missing_columns_on_upgraded_table(monkeypatch):
    conn = _FakeConn()
    # An already-created table missing everything added after it first shipped.
    conn.results = [[("decision_id",), ("tenant_id",), ("final_tier",)]]
    _make_store(monkeypatch, conn)
    altered = [sql for sql, _ in conn.calls if sql.startswith("ALTER TABLE decisions")]
    # Asserted against the module constant, not a copy of it: a new column
    # must be added by the migration, and the list cannot silently drift.
    assert [sql.split()[5] for sql in altered] == [
        column for column, _ in server._DECISION_COLUMN_ADDITIONS
    ]
    # Every addition carries a DEFAULT so pre-existing rows stay readable.
    assert all("DEFAULT" in sql or "NULL" in sql for sql in altered)


def test_mysqlstore_export_filters_biased_and_uncaptured_rows(monkeypatch):
    conn = _FakeConn()
    store = _make_store(monkeypatch, conn)
    conn.results = [
        [("s1", 2, 1_700_000_000_000, "fs-1", "Zg==", None, "R2", "c2",
          json.dumps({"c0": 0.1, "c1": 0.1, "c2": 0.7, "c3": 0.1}), 0.4, 0.9, 1, "v4", "d-1")]
    ]
    rows = store.export_training_rows("t1", 0, 10)
    sql = next(c[0] for c in conn.calls if "FROM decisions" in c[0] and "features_b64" in c[0])
    # The discard is enforced in SQL, so no reader can forget to apply it.
    assert "tainted = 0" in sql and "features_b64 IS NOT NULL" in sql
    # Rows come out in RouterTrainSample field shape, ready for the aligner.
    assert rows[0]["session_key"] == "s1"
    assert rows[0]["turn_index"] == 2
    assert rows[0]["features_390_b64"] == "Zg=="
    assert rows[0]["route_class"] == "R2"
    assert rows[0]["final_route_class"] == "R2"  # served tier IS the label
    assert rows[0]["routed_tier"] == "c2"
    assert rows[0]["probabilities"] == [0.1, 0.1, 0.7, 0.1]
    assert rows[0]["complaint_detected"] is True
    assert rows[0]["image_route"] is False
    assert rows[0]["decision_id"] == "d-1"
    assert rows[0]["ts"].endswith("Z")

