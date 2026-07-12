"""Contract tests for the standalone central routing service (services/squilla_central)."""

from __future__ import annotations

from services.squilla_central.server import (
    TEXT_TIERS,
    TIER_ANCHOR_TEXTS,
    Central,
    CentralStore,
    EmbeddingsConfig,
    flat_anchor_texts,
)

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
        store=CentralStore(":memory:"),
        embeddings=EmbeddingsConfig(url="http://stub"),
        policy_version="test-v1",
        token=token,
        embed_fn=embed_fn,
        now_ms=lambda: 1000,
    )


def route_body(message: str) -> dict:
    return {"tenantId": "t1", "sessionKey": "s1", "message": message, "attachmentCount": 0}


def test_routes_semantically_and_stores_no_plaintext():
    central = make_central()
    message = "c3:设计一个跨区域容灾架构-SECRET-PAYLOAD"
    status, body = central.handle("POST", "/v1/route", {}, route_body(message), None)
    assert status == 200
    assert body["tier"] == "c3"
    assert body["band"] == "semantic"

    stored = central.store.get_decision(body["decisionId"])
    assert stored["baseTier"] == "c3"
    assert stored["finalTier"] == "c3"
    assert stored["charLen"] == len(message)
    assert stored["policyVersion"] == "test-v1"
    assert len(stored["topAnchors"]) > 0
    assert "SECRET-PAYLOAD" not in str(stored)


def test_heuristic_fallback_when_embeddings_fail():
    central = make_central(embed_fn=embed_fail)
    status, body = central.handle("POST", "/v1/route", {}, route_body("谢谢"), None)
    assert status == 200
    assert body["tier"] == "c0"
    assert body["band"] == "short_plain"
    assert central.store.get_decision(body["decisionId"])["band"] == "short_plain"


def test_flag_upgrades_apply_centrally():
    central = make_central()
    status, body = central.handle(
        "POST", "/v1/route", {}, route_body("c0:把这个删除了直接部署到生产"), None
    )
    assert status == 200
    assert body["tier"] == "c2"
    assert body["flagUpgraded"] is True


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
