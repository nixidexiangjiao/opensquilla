"""Contract tests for the standalone SquillaRouter HTTP classification service."""

from __future__ import annotations

import httpx
import pytest

from opensquilla.squilla_router.http_service import build_app


class _StubStrategy:
    source = "v4_phase3"
    _model_version = "test-1"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def classify(self, message, valid_tiers, routing_history=None, **kwargs):
        self.calls.append({"message": message, "history": routing_history})
        return (
            "c2",
            0.83,
            "v4_phase3",
            {
                "route_class": "R2",
                "thinking_mode": "T2",
                "prompt_policy": "P1",
                "difficulty": 1.7,
                "margin": 0.4,
                "probabilities": {"R0": 0.02, "R1": 0.1, "R2": 0.83, "R3": 0.05},
                "model_version": "test-1",
            },
        )


class _UnavailableStrategy(_StubStrategy):
    async def classify(self, message, valid_tiers, routing_history=None, **kwargs):
        return ("c1", 0.0, "v4_unavailable", {"route_class": "R1"})


def _client(strategy, token=None) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=build_app(strategy, token))
    return httpx.AsyncClient(transport=transport, base_url="http://service")


@pytest.mark.asyncio
async def test_classify_returns_ml_decision():
    strategy = _StubStrategy()
    async with _client(strategy) as client:
        resp = await client.post(
            "/v1/classify",
            json={
                "message": "explain this traceback",
                "history": [
                    {"text": "earlier turn", "routeClass": "R1", "difficulty": 0.9, "margin": 0.3},
                    {"text": "ignored-extra-fields", "route_class": "R2", "bogus": [1, 2]},
                ],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "c2"
    assert body["routeClass"] == "R2"
    assert body["confidence"] == pytest.approx(0.83)
    assert body["thinkingMode"] == "T2"
    # History mapped onto the strategy's routing_history shape.
    history = strategy.calls[0]["history"]
    assert history[0] == {
        "text": "earlier turn",
        "route_class": "R1",
        "difficulty_score": 0.9,
        "margin": 0.3,
    }
    assert history[1]["route_class"] == "R2"


@pytest.mark.asyncio
async def test_classify_validates_body():
    async with _client(_StubStrategy()) as client:
        missing = await client.post("/v1/classify", json={"history": []})
        not_json = await client.post("/v1/classify", content=b"nope")
    assert missing.status_code == 400
    assert not_json.status_code == 400


@pytest.mark.asyncio
async def test_classify_requires_bearer_token_when_configured():
    async with _client(_StubStrategy(), token="s3cret") as client:
        denied = await client.post("/v1/classify", json={"message": "hi"})
        wrong = await client.post(
            "/v1/classify", json={"message": "hi"}, headers={"Authorization": "Bearer nope"}
        )
        allowed = await client.post(
            "/v1/classify", json={"message": "hi"}, headers={"Authorization": "Bearer s3cret"}
        )
    assert denied.status_code == 401
    assert wrong.status_code == 401
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_unavailable_runtime_returns_503():
    async with _client(_UnavailableStrategy()) as client:
        resp = await client.post("/v1/classify", json={"message": "hi"})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_healthz_reports_model_version():
    async with _client(_StubStrategy()) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["modelVersion"] == "test-1"
