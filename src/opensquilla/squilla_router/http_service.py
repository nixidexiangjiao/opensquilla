"""Standalone HTTP classification service for external SquillaRouter deployment.

Wraps ``V4Phase3Strategy.classify`` behind a minimal JSON-over-HTTP API so a
lightweight client (for example the OpenClaw ``squilla-router`` plugin) can run
on a constrained box and delegate ML routing to a machine that has the
``recommended`` runtime installed.

Run:
    python -m opensquilla.squilla_router.http_service --host 0.0.0.0 --port 8701

Endpoints:
    GET  /healthz      -> {"status": "ok", "modelVersion": ...}
    POST /v1/classify  -> {"tier", "routeClass", "confidence", ...}

Auth: set ``OPENSQUILLA_ROUTER_TOKEN`` to require ``Authorization: Bearer <token>``
on /v1/classify. Without it the service trusts its network boundary — bind to a
private interface or front it with your own proxy.

The service refuses to start when the ML runtime cannot load: a degraded
instance answering default tiers would silently disable the caller's own
heuristic fallback, which is strictly worse than failing fast.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import os
from typing import Any

import structlog
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from opensquilla.router_tiers import TEXT_TIERS
from opensquilla.squilla_router.v4_phase3 import V4Phase3Strategy

log = structlog.get_logger(__name__)

_MAX_BODY_BYTES = 256 * 1024
_MAX_HISTORY_ENTRIES = 5


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    prefix = "bearer "
    if header.lower().startswith(prefix):
        return header[len(prefix) :]
    return None


def _normalize_history(raw: object) -> list[dict[str, Any]]:
    """Map wire history entries onto the strategy's routing_history shape."""
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw[-_MAX_HISTORY_ENTRIES:]:
        if not isinstance(item, dict):
            continue
        route_class = item.get("routeClass") or item.get("route_class")
        entry: dict[str, Any] = {"text": str(item.get("text") or "")}
        if route_class:
            entry["route_class"] = str(route_class)
        for wire_key, strategy_key in (
            ("difficulty", "difficulty_score"),
            ("margin", "margin"),
        ):
            value = item.get(wire_key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                entry[strategy_key] = float(value)
        entries.append(entry)
    return entries


class ClassifyService:
    def __init__(self, strategy: V4Phase3Strategy, token: str | None) -> None:
        self._strategy = strategy
        self._token = token
        # The ONNX/LightGBM sessions are shared; serialize predict calls so a
        # burst of requests cannot interleave inside the native runtimes.
        self._classify_lock = asyncio.Lock()

    async def healthz(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "source": self._strategy.source,
                "modelVersion": getattr(self._strategy, "_model_version", "unknown"),
            }
        )

    async def classify(self, request: Request) -> JSONResponse:
        if self._token is not None:
            provided = _bearer_token(request) or ""
            if not hmac.compare_digest(provided, self._token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)

        body = await request.body()
        if len(body) > _MAX_BODY_BYTES:
            return JSONResponse({"error": "body too large"}, status_code=413)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON from the network
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return JSONResponse({"error": "message must be a non-empty string"}, status_code=400)

        routing_history = _normalize_history(payload.get("history"))
        async with self._classify_lock:
            tier, confidence, source, extra = await self._strategy.classify(
                message,
                list(TEXT_TIERS),
                routing_history=routing_history or None,
            )

        if source == "v4_unavailable":
            # The runtime broke after startup; tell the caller to use its own
            # fallback rather than serving default-tier answers as ML output.
            return JSONResponse({"error": "router runtime unavailable"}, status_code=503)

        return JSONResponse(
            {
                "tier": tier,
                "routeClass": extra.get("route_class"),
                "confidence": confidence,
                "source": source,
                "thinkingMode": extra.get("thinking_mode"),
                "promptPolicy": extra.get("prompt_policy"),
                "difficulty": extra.get("difficulty"),
                "margin": extra.get("margin"),
                "probabilities": extra.get("probabilities"),
                "modelVersion": extra.get("model_version"),
            }
        )


def build_app(strategy: V4Phase3Strategy, token: str | None) -> Starlette:
    service = ClassifyService(strategy, token)
    return Starlette(
        routes=[
            Route("/healthz", service.healthz, methods=["GET"]),
            Route("/v1/classify", service.classify, methods=["POST"]),
        ]
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SquillaRouter HTTP classification service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8701)
    parser.add_argument(
        "--bundle-dir",
        default=None,
        help="V4 inference bundle directory (defaults to the packaged bundle)",
    )
    args = parser.parse_args(argv)

    # require_router_runtime=True: fail fast when the ML runtime cannot load.
    strategy = V4Phase3Strategy(bundle_dir=args.bundle_dir, require_router_runtime=True)
    token = os.environ.get("OPENSQUILLA_ROUTER_TOKEN") or None
    log.info(
        "squilla_router.http_service.start",
        host=args.host,
        port=args.port,
        auth="bearer" if token else "none",
    )
    uvicorn.run(build_app(strategy, token), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
