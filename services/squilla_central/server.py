"""Central SquillaRouter service: ALL routing intelligence lives here.

The OpenClaw ``squilla-router`` plugin is a thin client — it POSTs the message
text and gets an abstract tier back. This service embeds the message (external
OpenAI-compatible embeddings endpoint), classifies it against the tier anchor
prompts, runs the confidence gate and flag upgrades, records a plaintext-free
decision trail keyed by decisionId in MySQL, and accepts feedback for later
self-learning. Stdlib plus PyMySQL — ``pip install PyMySQL`` on the box.

Generic wire contract: the client depends ONLY on ``tier`` (an abstract
capability tier ``c0``-``c3``, cheap->strong, that the client maps to a model)
plus ``decisionId``. Everything algorithm-specific rides under opaque ``meta``,
which the client logs but never parses. So the classifier below can be swapped
for any other implementation with no client change — see the upgrade seam.

Privacy contract: message text exists only in the request; it is never written
to the store. The ``decisions`` schema has no text column — only derived data
(char length, flags, probabilities, the base->gated->final tier trail, nearest
anchors, the embedding vector). Debugging correlates through decisionId: the
client logs it next to its own transcript, which is where the plaintext lives.

Upgrade seam: ``classify_semantic`` is the single classification entry point.
To switch from zero-training anchor similarity to OpenSquilla's trained V4
pipeline (or any other router), replace its body — the store, trail, endpoints,
and wire contract all stay as they are, as long as it returns ``final_tier``.

Run:
    SQUILLA_EMBEDDINGS_URL=http://ml-box:8080/v1/embeddings \
    SQUILLA_MYSQL_HOST=db-box SQUILLA_MYSQL_USER=squilla \
    SQUILLA_MYSQL_PASSWORD=... SQUILLA_MYSQL_DATABASE=squilla_central \
    SQUILLA_CENTRAL_TOKEN=<token> \
    python3 services/squilla_central/server.py --host 0.0.0.0 --port 8710

Wire contract (mirrored by the OpenClaw plugin's central-client.ts):
    POST /v1/route      {tenantId, sessionKey, profile?, message, attachmentCount}
                        -> {decisionId, tier, confidence, policyVersion,
                            meta: {...algorithm-specific, opaque to client}}
    POST /v1/feedback   {decisionId, rating: up|down|neutral}
    GET  /v1/decisions/{id} | /v1/decisions?tenantId&sessionKey&limit
    GET  /v1/stats?tenantId
    GET  /healthz
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

TEXT_TIERS = ("c0", "c1", "c2", "c3")
ROUTE_CLASSES = ("R0", "R1", "R2", "R3")

# ---------------------------------------------------------------------------
# Anchors (must stay identical to extensions/squilla-router/semantic.ts so the
# central service and any offline analysis agree on the anchor space).
# ---------------------------------------------------------------------------

TIER_ANCHOR_TEXTS: dict[str, list[str]] = {
    "c0": [
        "谢谢",
        "好的，收到",
        "thanks, that works",
        "ok sounds good",
        "把这句话改得礼貌一点",
        "这个词是什么意思",
        "translate this sentence to English",
        "今天是星期几",
    ],
    "c1": [
        "帮我写一封请假邮件",
        "这段代码是做什么的",
        "给这个函数加上注释",
        "对比一下这两个方案的优缺点",
        "write a regex that matches email addresses",
        "总结一下这篇文章的要点",
        "how do I sort a list of objects in Python",
        "帮我把这段介绍润色得更正式",
    ],
    "c2": [
        "这个报错是什么原因，帮我修复",
        "为什么这个测试在 CI 上失败，本地却能通过",
        "帮我实现一个带重试和超时控制的下载函数",
        "分析这段日志，找出请求变慢的根因",
        "debug this stack trace and explain the root cause",
        "设计这个功能的实现步骤并列出要改动的文件",
        "这个内存泄漏应该怎么排查",
        "refactor this module to remove the circular dependency",
    ],
    "c3": [
        "设计一个跨区域容灾的部署架构",
        "评估从单体迁移到微服务的方案和风险",
        "怎么安全地把生产数据库迁移到新集群",
        "design a multi-tenant authorization architecture",
        "制定这个系统的分库分表和数据迁移方案",
        "评估这两种一致性协议在我们场景下的取舍",
        "规划一次零停机的大版本升级",
        "audit this design for security and scalability risks",
    ],
}


def flat_anchor_texts() -> list[str]:
    return [text for tier in TEXT_TIERS for text in TIER_ANCHOR_TEXTS[tier]]


# ---------------------------------------------------------------------------
# Rule constants (OpenSquilla router.runtime.yaml / heuristic.py parity; keep
# in sync with extensions/squilla-router/router.ts, the plugin-side fallback).
# ---------------------------------------------------------------------------

SCORE_TEMPERATURE = 0.05
TOP_K_ANCHORS = 2
TOP_ANCHORS_REPORTED = 3
MARGIN_UPGRADE_THRESHOLD = 0.10
UNDER_ROUTING_SAFETY_THRESHOLD = 0.45

HEAVY_MIN_CHARS = 12_000
HEAVY_MIN_FENCED_BLOCKS = 3
CODE_OR_MATERIAL_MIN_CHARS = 2_500
SHORT_PLAIN_MAX_CHARS = 240
MEDIUM_PLAIN_MAX_CHARS = 1_200
CONFIDENT_HIGH = 0.6
CONFIDENT_LOW = 0.55
BORDERLINE = 0.4

HIGH_RISK_KEYWORDS = [
    "生产", "部署", "回滚", "迁移", "删除", "客户", "法务", "财务",
    "deploy", "rollback", "migration", "delete", "overwrite", "production",
    "customer-facing",
]
DEBUG_KEYWORDS = [
    "error", "bug", "exception", "traceback", "failed", "root cause",
    "报错", "根因", "修复",
]
DEBUG_PATTERNS = [
    re.compile(r"Traceback \(most recent"),
    re.compile(r"stderr:"),
    re.compile(r"FAILED"),
]
REPO_ARCH_KEYWORDS = [
    "repo", "codebase", "monorepo", "architecture", "重构", "架构", "module",
    "dependency",
]
STRICT_FORMAT_KEYWORDS = ["json", "yaml", "csv", "schema", "只返回", "不要解释", "按格式"]
LONG_CONTEXT_CHAR_THRESHOLD = 6_000
LONG_CONTEXT_CODE_BLOCK_THRESHOLD = 1_500
LONG_CONTEXT_LOG_BLOCK_THRESHOLD = 1_500
LONG_CONTEXT_FILE_REF_THRESHOLD = 2
CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
LOG_BLOCK_RE = re.compile(
    r"(\d{4}[-/]\d{2}[-/]\d{2}[\sT]\d{2}:\d{2}.*\n){3,}"
    r"|(^\[?(INFO|WARN|ERROR|DEBUG)\]?\s.*\n){3,}",
    re.MULTILINE,
)
FILE_PATH_RE = re.compile(r"(?:^|[\s\"'`(])([a-zA-Z_][\w.-]*/[\w./-]+\.\w+)", re.MULTILINE)

RATINGS = {"up", "down", "neutral"}
MAX_MESSAGE_CHARS = 64_000
LIST_LIMIT_MAX = 100


# ---------------------------------------------------------------------------
# Classification (pure functions; port of semantic.ts + router.ts)
# ---------------------------------------------------------------------------


def compute_flags(message: str) -> dict[str, bool]:
    lower = message.lower()

    def has_any(keywords: list[str]) -> bool:
        return any(keyword.lower() in lower for keyword in keywords)

    code_len = sum(len(match.group()) for match in CODE_BLOCK_RE.finditer(message))
    log_len = sum(len(match.group()) for match in LOG_BLOCK_RE.finditer(message))
    return {
        "highRisk": has_any(HIGH_RISK_KEYWORDS),
        "debug": has_any(DEBUG_KEYWORDS) or any(p.search(message) for p in DEBUG_PATTERNS),
        "repoArch": has_any(REPO_ARCH_KEYWORDS),
        "strictFormat": has_any(STRICT_FORMAT_KEYWORDS),
        "longContext": (
            len(message) >= LONG_CONTEXT_CHAR_THRESHOLD
            or code_len >= LONG_CONTEXT_CODE_BLOCK_THRESHOLD
            or log_len >= LONG_CONTEXT_LOG_BLOCK_THRESHOLD
            or len(FILE_PATH_RE.findall(message)) >= LONG_CONTEXT_FILE_REF_THRESHOLD
        ),
    }


def apply_flag_upgrades(tier: str, flags: dict[str, bool]) -> str:
    idx = TEXT_TIERS.index(tier)
    if flags["highRisk"]:
        idx = max(idx, 2)
    if flags["debug"] and flags["longContext"]:
        idx = max(idx, 2)
    if flags["repoArch"]:
        idx = max(idx, 1)
    return TEXT_TIERS[idx]


def classify_heuristic(message: str, attachment_count: int) -> dict[str, Any]:
    """Band heuristic (OpenSquilla heuristic.py); the no-embeddings fallback."""
    char_len = len(message)
    fenced = message.count("```") // 2
    if char_len >= HEAVY_MIN_CHARS or fenced >= HEAVY_MIN_FENCED_BLOCKS:
        band, tier, confidence = "heavy", "c3", CONFIDENT_HIGH
    elif fenced > 0 or char_len >= CODE_OR_MATERIAL_MIN_CHARS or attachment_count > 0:
        band, tier, confidence = "code_or_material", "c2", CONFIDENT_HIGH
    elif char_len <= SHORT_PLAIN_MAX_CHARS:
        band, tier, confidence = "short_plain", "c0", CONFIDENT_LOW
    elif char_len <= MEDIUM_PLAIN_MAX_CHARS:
        band, tier, confidence = "medium_plain", "c1", CONFIDENT_LOW
    else:
        band, tier, confidence = "borderline_plain", "c1", BORDERLINE
    flags = compute_flags(message)
    final = apply_flag_upgrades(tier, flags)
    return {
        "band": band,
        "base_tier": tier,
        "gated_tier": tier,
        "final_tier": final,
        "confidence": confidence,
        "margin": 0.0,
        "probabilities": {t: 0.0 for t in TEXT_TIERS},
        "flags": flags,
        "flag_upgraded": final != tier,
        "top_anchors": [],
        "embedding": None,
    }


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm > 0 else 0.0


def _softmax(scores: list[float], temperature: float) -> list[float]:
    scaled = [score / temperature for score in scores]
    peak = max(scaled)
    exps = [math.exp(value - peak) for value in scaled]
    total = sum(exps)
    return [value / total for value in exps]


def classify_semantic(
    message: str,
    query_vector: list[float],
    anchor_vectors: list[list[float]],
    gate: dict[str, Any],
) -> dict[str, Any]:
    """Anchor-similarity classification + OpenSquilla postprocess + gate + flags.

    This is the seam to swap for ``V4Phase3Strategy.classify`` later: return
    the same dict shape and nothing else needs to change.
    """
    anchor_texts = flat_anchor_texts()
    sims = [_cosine(query_vector, anchor) for anchor in anchor_vectors]
    tier_scores: list[float] = []
    offset = 0
    for tier in TEXT_TIERS:
        count = len(TIER_ANCHOR_TEXTS[tier])
        tier_sims = sorted(sims[offset : offset + count], reverse=True)[:TOP_K_ANCHORS]
        tier_scores.append(sum(tier_sims) / len(tier_sims))
        offset += count
    probs = _softmax(tier_scores, SCORE_TEMPERATURE)
    ranked = sorted(range(len(probs)), key=lambda i: probs[i], reverse=True)
    confidence = probs[ranked[0]]
    margin = probs[ranked[0]] - probs[ranked[1]]
    tier_idx = ranked[0]

    # OpenSquilla postprocess order: margin upgrade, then under-routing safety.
    if margin < MARGIN_UPGRADE_THRESHOLD:
        tier_idx = min(tier_idx + 1, len(TEXT_TIERS) - 1)
    if tier_idx < 2 and probs[2] + probs[3] > UNDER_ROUTING_SAFETY_THRESHOLD:
        tier_idx = 2

    base_tier = TEXT_TIERS[tier_idx]
    gated_tier = gate["default_tier"] if confidence < gate["confidence_threshold"] else base_tier
    flags = compute_flags(message)
    final_tier = apply_flag_upgrades(gated_tier, flags)
    ranked_anchor_idx = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)
    top_anchors = [
        {"text": anchor_texts[i], "similarity": sims[i]}
        for i in ranked_anchor_idx[:TOP_ANCHORS_REPORTED]
    ]
    return {
        "band": "semantic",
        "base_tier": base_tier,
        "gated_tier": gated_tier,
        "final_tier": final_tier,
        "confidence": confidence,
        "margin": margin,
        "probabilities": dict(zip(TEXT_TIERS, probs)),
        "flags": flags,
        "flag_upgraded": final_tier != gated_tier,
        "top_anchors": top_anchors,
        "embedding": query_vector,
    }


# ---------------------------------------------------------------------------
# Embeddings client (OpenAI-compatible, urllib; no deps)
# ---------------------------------------------------------------------------


@dataclass
class EmbeddingsConfig:
    url: str
    model: str = "bge-small-zh-v1.5"
    api_key: str | None = None
    timeout_s: float = 2.0


def embed_texts(config: EmbeddingsConfig, texts: list[str]) -> list[list[float]] | None:
    """Return vectors in input order, or None on any failure (caller falls back)."""
    payload = json.dumps({"model": config.model, "input": texts}).encode("utf-8")
    request = urllib.request.Request(
        config.url,
        data=payload,
        headers={
            "content-type": "application/json",
            **({"authorization": f"Bearer {config.api_key}"} if config.api_key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_s) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    data = body.get("data")
    if not isinstance(data, list) or len(data) != len(texts):
        return None
    vectors: list[list[float] | None] = [None] * len(texts)
    for position, item in enumerate(data):
        if not isinstance(item, dict):
            return None
        embedding = item.get("embedding")
        index = item.get("index", position)
        if (
            not isinstance(embedding, list)
            or not embedding
            or not isinstance(index, int)
            or not 0 <= index < len(texts)
            or vectors[index] is not None
        ):
            return None
        vectors[index] = [float(v) for v in embedding]
    dimension = len(vectors[0] or [])
    if any(vector is None or len(vector) != dimension for vector in vectors):
        return None
    return vectors  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Store (MySQL via PyMySQL; NO plaintext column by design)
# ---------------------------------------------------------------------------

# MySQL DDL. utf8mb4 so CJK anchors/tenant ids round-trip; JSON blobs live in
# LONGTEXT (portable across MySQL 5.7/8 and MariaDB, and the embedding vector
# can be large). Placeholders below are DB-API "%s" (PyMySQL), not "?".
_DDL_DECISIONS = """
CREATE TABLE IF NOT EXISTS decisions (
  decision_id VARCHAR(64) NOT NULL PRIMARY KEY,
  tenant_id VARCHAR(191) NOT NULL,
  session_key VARCHAR(191) NOT NULL,
  profile VARCHAR(191) NOT NULL DEFAULT '',
  ts_ms BIGINT NOT NULL,
  band VARCHAR(32) NOT NULL,
  base_tier VARCHAR(8) NOT NULL,
  gated_tier VARCHAR(8) NOT NULL,
  final_tier VARCHAR(8) NOT NULL,
  confidence DOUBLE NOT NULL,
  margin DOUBLE NOT NULL,
  probabilities LONGTEXT NOT NULL,
  flags LONGTEXT NOT NULL,
  char_len INT NOT NULL,
  attachment_count INT NOT NULL,
  top_anchors LONGTEXT NOT NULL,
  policy_version VARCHAR(64) NOT NULL,
  latency_ms INT NOT NULL,
  embedding LONGTEXT NULL,
  INDEX idx_decisions_session (tenant_id, session_key, ts_ms)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
_DDL_FEEDBACK = """
CREATE TABLE IF NOT EXISTS feedback (
  decision_id VARCHAR(64) NOT NULL PRIMARY KEY,
  rating VARCHAR(16) NOT NULL,
  ts_ms BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_SUMMARY_COLUMNS = (
    "decision_id, tenant_id, session_key, profile, ts_ms, band, base_tier, gated_tier, "
    "final_tier, confidence, margin, probabilities, flags, char_len, "
    "attachment_count, top_anchors, policy_version, latency_ms"
)


@dataclass
class MySqlConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "squilla_central"


class MySqlStore:
    """Per-tenant decision + feedback store on MySQL.

    A single connection guarded by a lock (the service is low-QPS and every op
    is short); ``ping(reconnect=True)`` before each op survives MySQL's
    ``wait_timeout``. PyMySQL is imported lazily so this module stays importable
    (for tests using a fake store) on hosts without the driver.
    """

    def __init__(self, config: MySqlConfig) -> None:
        import pymysql  # lazy: only the deployed service needs the driver

        self._pymysql = pymysql
        self._config = config
        self._lock = threading.Lock()
        self._conn = self._connect()
        with self._lock:
            with self._conn.cursor() as cursor:
                cursor.execute(_DDL_DECISIONS)
                cursor.execute(_DDL_FEEDBACK)
            self._conn.commit()

    def _connect(self):
        return self._pymysql.connect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            database=self._config.database,
            charset="utf8mb4",
            autocommit=False,
        )

    def _cursor(self):
        self._conn.ping(reconnect=True)
        return self._conn.cursor()

    def insert_decision(self, record: dict[str, Any]) -> None:
        with self._lock, self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO decisions VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record["decisionId"],
                    record["tenantId"],
                    record["sessionKey"],
                    record["profile"],
                    record["tsMs"],
                    record["band"],
                    record["baseTier"],
                    record["gatedTier"],
                    record["finalTier"],
                    record["confidence"],
                    record["margin"],
                    json.dumps(record["probabilities"]),
                    json.dumps(record["flags"]),
                    record["charLen"],
                    record["attachmentCount"],
                    json.dumps(record["topAnchors"], ensure_ascii=False),
                    record["policyVersion"],
                    record["latencyMs"],
                    json.dumps(record["embedding"]) if record["embedding"] is not None else None,
                ),
            )
            self._conn.commit()

    def _summary(self, row: tuple, rating: str | None) -> dict[str, Any]:
        return {
            "decisionId": row[0],
            "tenantId": row[1],
            "sessionKey": row[2],
            "profile": row[3],
            "tsMs": row[4],
            "band": row[5],
            "baseTier": row[6],
            "gatedTier": row[7],
            "finalTier": row[8],
            "confidence": row[9],
            "margin": row[10],
            "probabilities": json.loads(row[11]),
            "flags": json.loads(row[12]),
            "charLen": row[13],
            "attachmentCount": row[14],
            "topAnchors": json.loads(row[15]),
            "policyVersion": row[16],
            "latencyMs": row[17],
            "rating": rating,
        }

    def _rating(self, cursor, decision_id: str) -> str | None:
        cursor.execute("SELECT rating FROM feedback WHERE decision_id = %s", (decision_id,))
        row = cursor.fetchone()
        return row[0] if row else None

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock, self._cursor() as cursor:
            cursor.execute(
                f"SELECT {_SUMMARY_COLUMNS} FROM decisions WHERE decision_id = %s",
                (decision_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._summary(row, self._rating(cursor, decision_id))

    def list_decisions(
        self, tenant_id: str, session_key: str | None, limit: int
    ) -> list[dict[str, Any]]:
        with self._lock, self._cursor() as cursor:
            if session_key:
                cursor.execute(
                    f"SELECT {_SUMMARY_COLUMNS} FROM decisions "
                    "WHERE tenant_id = %s AND session_key = %s ORDER BY ts_ms DESC LIMIT %s",
                    (tenant_id, session_key, limit),
                )
            else:
                cursor.execute(
                    f"SELECT {_SUMMARY_COLUMNS} FROM decisions "
                    "WHERE tenant_id = %s ORDER BY ts_ms DESC LIMIT %s",
                    (tenant_id, limit),
                )
            rows = cursor.fetchall()
            return [self._summary(row, self._rating(cursor, row[0])) for row in rows]

    def record_feedback(self, decision_id: str, rating: str, ts_ms: int) -> bool:
        with self._lock, self._cursor() as cursor:
            cursor.execute("SELECT 1 FROM decisions WHERE decision_id = %s", (decision_id,))
            if cursor.fetchone() is None:
                return False
            cursor.execute(
                "INSERT INTO feedback (decision_id, rating, ts_ms) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE rating = VALUES(rating), ts_ms = VALUES(ts_ms)",
                (decision_id, rating, ts_ms),
            )
            self._conn.commit()
            return True

    def stats(self, tenant_id: str) -> dict[str, Any]:
        with self._lock, self._cursor() as cursor:
            cursor.execute(
                "SELECT final_tier, COUNT(*) FROM decisions WHERE tenant_id = %s "
                "GROUP BY final_tier",
                (tenant_id,),
            )
            tiers = cursor.fetchall()
            cursor.execute(
                "SELECT band, COUNT(*) FROM decisions WHERE tenant_id = %s GROUP BY band",
                (tenant_id,),
            )
            bands = cursor.fetchall()
            cursor.execute(
                "SELECT profile, COUNT(*) FROM decisions WHERE tenant_id = %s GROUP BY profile",
                (tenant_id,),
            )
            profiles = cursor.fetchall()
            cursor.execute(
                "SELECT f.rating, COUNT(*) FROM feedback f "
                "JOIN decisions d ON d.decision_id = f.decision_id "
                "WHERE d.tenant_id = %s GROUP BY f.rating",
                (tenant_id,),
            )
            ratings = cursor.fetchall()
        return {
            "tiers": {row[0]: row[1] for row in tiers},
            "bands": {row[0]: row[1] for row in bands},
            "profiles": {row[0]: row[1] for row in profiles},
            "ratings": {row[0]: row[1] for row in ratings},
        }

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Service core (framework-free, unit-testable without HTTP)
# ---------------------------------------------------------------------------


class Central:
    def __init__(
        self,
        *,
        store: Any,  # any object with the MySqlStore method surface (tests inject a fake)
        embeddings: EmbeddingsConfig,
        default_tier: str = "c1",
        confidence_threshold: float = 0.5,
        policy_version: str = "central-py-v1",
        token: str | None = None,
        embed_fn: Any = None,
        now_ms: Any = None,
    ) -> None:
        self.store = store
        self.embeddings = embeddings
        self.default_tier = default_tier
        self.confidence_threshold = confidence_threshold
        self.policy_version = policy_version
        self.token = token
        # Stored on the instance (not the class) so plain functions never turn
        # into bound methods; both are injectable for tests.
        self.embed_fn = embed_fn if embed_fn is not None else embed_texts
        self.now_ms = now_ms if now_ms is not None else (lambda: int(time.time() * 1000))
        self._anchor_vectors: list[list[float]] | None = None
        self._anchor_lock = threading.Lock()

    # Anchors are constants; embed once per process. A failed load retries on
    # the next request instead of poisoning the cache.
    def _anchors(self) -> list[list[float]] | None:
        with self._anchor_lock:
            if self._anchor_vectors is None:
                self._anchor_vectors = self.embed_fn(self.embeddings, flat_anchor_texts())
            return self._anchor_vectors

    def handle(
        self, method: str, path: str, query: dict[str, list[str]], body: Any, auth: str | None
    ) -> tuple[int, dict[str, Any]]:
        if path == "/healthz" and method == "GET":
            return 200, {
                "status": "ok",
                "policyVersion": self.policy_version,
                "anchorsReady": self._anchor_vectors is not None,
            }
        if self.token is not None:
            provided = (auth or "").removeprefix("Bearer ").removeprefix("bearer ")
            if not hmac.compare_digest(provided, self.token):
                return 401, {"error": "unauthorized"}
        if path == "/v1/route" and method == "POST":
            return self._route(body)
        if path == "/v1/feedback" and method == "POST":
            return self._feedback(body)
        match = re.fullmatch(r"/v1/decisions/([\w-]+)", path)
        if match and method == "GET":
            decision = self.store.get_decision(match.group(1))
            return (200, decision) if decision else (404, {"error": "unknown decisionId"})
        if path == "/v1/decisions" and method == "GET":
            tenant_id = (query.get("tenantId") or [None])[0]
            if not tenant_id:
                return 400, {"error": "tenantId is required"}
            try:
                limit = int((query.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(limit, LIST_LIMIT_MAX))
            session_key = (query.get("sessionKey") or [None])[0]
            return 200, {"decisions": self.store.list_decisions(tenant_id, session_key, limit)}
        if path == "/v1/stats" and method == "GET":
            tenant_id = (query.get("tenantId") or [None])[0]
            if not tenant_id:
                return 400, {"error": "tenantId is required"}
            return 200, self.store.stats(tenant_id)
        return 404, {"error": "not found"}

    def _route(self, body: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(body, dict):
            return 400, {"error": "body must be a JSON object"}
        tenant_id = body.get("tenantId")
        message = body.get("message")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            return 400, {"error": "tenantId is required"}
        if not isinstance(message, str) or not message.strip():
            return 400, {"error": "message must be a non-empty string"}
        if len(message) > MAX_MESSAGE_CHARS:
            return 413, {"error": "message too large"}
        session_key = body.get("sessionKey") if isinstance(body.get("sessionKey"), str) else ""
        # Which client-side routing profile (virtual model id) triggered this
        # turn. Free-form string: keeps the wire contract generic while letting
        # stats and future policy key on it.
        profile = body.get("profile") if isinstance(body.get("profile"), str) else ""
        raw_attachments = body.get("attachmentCount")
        attachment_count = (
            int(raw_attachments)
            if isinstance(raw_attachments, (int, float)) and raw_attachments > 0
            else 0
        )

        started = self.now_ms()
        # Semantic first; central-side heuristic when embeddings are down, so
        # clients only use their local fallback when THIS service is down.
        anchors = self._anchors()
        query_vectors = self.embed_fn(self.embeddings, [message]) if anchors else None
        if anchors and query_vectors:
            gate = {
                "default_tier": self.default_tier,
                "confidence_threshold": self.confidence_threshold,
            }
            outcome = classify_semantic(message, query_vectors[0], anchors, gate)
        else:
            outcome = classify_heuristic(message, attachment_count)

        record = {
            "decisionId": str(uuid.uuid4()),
            "tenantId": tenant_id,
            "sessionKey": session_key,
            "profile": profile,
            "tsMs": started,
            "band": outcome["band"],
            "baseTier": outcome["base_tier"],
            "gatedTier": outcome["gated_tier"],
            "finalTier": outcome["final_tier"],
            "confidence": outcome["confidence"],
            "margin": outcome["margin"],
            "probabilities": outcome["probabilities"],
            "flags": outcome["flags"],
            "charLen": len(message),
            "attachmentCount": attachment_count,
            "topAnchors": outcome["top_anchors"],
            "policyVersion": self.policy_version,
            "latencyMs": self.now_ms() - started,
            "embedding": outcome["embedding"],
        }
        self.store.insert_decision(record)
        final_tier = outcome["final_tier"]
        # Generic wire response: `tier` (the abstract c0-c3 capability tier the
        # client maps to a model) + confidence are the only contract; everything
        # algorithm-specific goes under opaque `meta`, which the client logs but
        # never branches on. Swapping the classifier keeps this shape unchanged.
        return 200, {
            "decisionId": record["decisionId"],
            "tier": final_tier,
            "confidence": outcome["confidence"],
            "policyVersion": self.policy_version,
            "meta": {
                "routeClass": ROUTE_CLASSES[TEXT_TIERS.index(final_tier)],
                "band": outcome["band"],
                "flags": outcome["flags"],
                "flagUpgraded": outcome["flag_upgraded"],
                "margin": outcome["margin"],
            },
        }

    def _feedback(self, body: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(body, dict):
            return 400, {"error": "body must be a JSON object"}
        decision_id = body.get("decisionId")
        rating = body.get("rating")
        if (
            not isinstance(decision_id, str)
            or not decision_id
            or not isinstance(rating, str)
            or rating not in RATINGS
        ):
            return 400, {"error": "decisionId and rating (up|down|neutral) are required"}
        recorded = self.store.record_feedback(decision_id, rating, self.now_ms())
        return (200, {"ok": True}) if recorded else (404, {"error": "unknown decisionId"})


# ---------------------------------------------------------------------------
# HTTP shell
# ---------------------------------------------------------------------------

MAX_BODY_BYTES = 256 * 1024


def make_handler(central: Central) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, body: Any) -> None:
            url = urlparse(self.path)
            status, payload = central.handle(
                self.command,
                url.path,
                parse_qs(url.query),
                body,
                self.headers.get("authorization"),
            )
            self._respond(status, payload)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            self._dispatch(None)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY_BYTES:
                self._respond(413, {"error": "body too large"})
                return
            try:
                body = json.loads(self.rfile.read(length)) if length else None
            except ValueError:
                self._respond(400, {"error": "invalid JSON body"})
                return
            self._dispatch(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            # decisionIds land in the store; request logging stays quiet so
            # message text never hits server logs either.
            pass

    return Handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SquillaRouter central routing service")
    parser.add_argument("--host", default=os.environ.get("SQUILLA_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SQUILLA_PORT", "8710")))
    args = parser.parse_args(argv)

    embeddings_url = os.environ.get("SQUILLA_EMBEDDINGS_URL")
    if not embeddings_url:
        raise SystemExit("SQUILLA_EMBEDDINGS_URL is required")
    central = Central(
        store=MySqlStore(
            MySqlConfig(
                host=os.environ.get("SQUILLA_MYSQL_HOST", "127.0.0.1"),
                port=int(os.environ.get("SQUILLA_MYSQL_PORT", "3306")),
                user=os.environ.get("SQUILLA_MYSQL_USER", "root"),
                password=os.environ.get("SQUILLA_MYSQL_PASSWORD", ""),
                database=os.environ.get("SQUILLA_MYSQL_DATABASE", "squilla_central"),
            )
        ),
        embeddings=EmbeddingsConfig(
            url=embeddings_url,
            model=os.environ.get("SQUILLA_EMBEDDINGS_MODEL", "bge-small-zh-v1.5"),
            api_key=os.environ.get("SQUILLA_EMBEDDINGS_API_KEY"),
            timeout_s=float(os.environ.get("SQUILLA_EMBEDDINGS_TIMEOUT_S", "2.0")),
        ),
        default_tier=(
            os.environ.get("SQUILLA_DEFAULT_TIER", "c1")
            if os.environ.get("SQUILLA_DEFAULT_TIER", "c1") in TEXT_TIERS
            else "c1"
        ),
        confidence_threshold=float(os.environ.get("SQUILLA_CONFIDENCE_THRESHOLD", "0.5")),
        policy_version=os.environ.get("SQUILLA_POLICY_VERSION", "central-py-v1"),
        token=os.environ.get("SQUILLA_CENTRAL_TOKEN") or None,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(central))
    print(f"squilla-central listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
