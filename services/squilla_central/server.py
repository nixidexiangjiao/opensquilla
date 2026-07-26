"""Central SquillaRouter service: ALL routing intelligence lives here.

The OpenClaw ``squilla-router`` plugin is a PURE PASS-THROUGH client — it POSTs
the turn and applies the tier it gets back, making no routing judgment of its
own (no classification, no KV-cache sticky, no image handling, no tier
snapping). All of that lives here. This service runs OpenSquilla's real V4
Phase 3 model (the trained BGE-ONNX + LightGBM + MLP ensemble, via
``V4Phase3Strategy``), records a plaintext-free decision trail keyed by
decisionId in MySQL, and accepts feedback for later self-learning. If the V4
model bundle or its ML deps are unavailable it degrades to the dependency-free
band heuristic (``classify_heuristic``) so routing still answers.

Because the client applies the returned tier verbatim, ``decisions.final_tier``
is exactly the tier that was served — a truthful self-learning label.
``classifier_tier`` keeps the model's own pick for diagnostics.

Deps: PyMySQL always; the V4 path additionally needs ``opensquilla[recommended]``
(numpy / lightgbm / onnxruntime / scikit-learn / joblib) plus the Git-LFS model
bundle under ``opensquilla/squilla_router/models/v4.2_phase3_inference`` (run
``git lfs pull``). No external embeddings endpoint — BGE runs in-process (ONNX).

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

Classifier seam: the injected ``classifier`` is the single classification entry
point (``V4Classifier`` in production, a fake in tests, ``None`` to force the
heuristic). Swapping the router = swapping the classifier — the store, trail,
endpoints, and wire contract all stay as they are, as long as it returns an
outcome dict with ``final_tier``.

Run:
    SQUILLA_MYSQL_HOST=db-box SQUILLA_MYSQL_USER=squilla \
    SQUILLA_MYSQL_PASSWORD=... SQUILLA_MYSQL_DATABASE=squilla_central \
    SQUILLA_CENTRAL_TOKEN=<token> \
    PYTHONPATH=src python3 services/squilla_central/server.py --host 0.0.0.0 --port 8710
    # SQUILLA_V4=0 forces the heuristic fallback (no ML deps / bundle needed).

Wire contract (mirrored by the OpenClaw plugin's central-client.ts):
    POST /v1/route      {tenantId, sessionKey, profile?, message, attachmentCount,
                         hasImage?, availableTiers?}
                        -> {decisionId, tier, confidence, policyVersion,
                            meta: {...algorithm-specific, opaque to client}}
                        `tier` is the SERVED tier: the client is pure
                        pass-through and applies it verbatim.
    POST /v1/feedback   {decisionId, rating: up|down|neutral}
    GET  /v1/decisions/{id} | /v1/decisions?tenantId&sessionKey&limit
    GET  /v1/stats?tenantId
    GET  /v1/train/export?tenantId&sinceMs&limit
                        -> {samples: [...RouterTrainSample...], count}
    GET  /v1/policy | POST /v1/policy/reload | POST /v1/policy/simulate
    GET  /healthz

Self-learning capture: every classified turn stores the exact 390-dim feature
vector the heads consumed plus its ``feature_schema_version``, the raw route
class, a per-session ``turn_index``, and a ``complaint`` boolean derived from
the message (never the message). Those are what turn a stream of decisions into
a trainable corpus; ``/v1/train/export`` emits them in RouterTrainSample shape.

Operator control: see ``policy.py``. Rules can nudge the tier probabilities or
hard-bound the served tier (floor/ceiling/pin), scoped by tenant, profile, UTC
hour window, validity dates, and a rollout ratio, with dry-run and per-rule
impact counters. A turn whose SERVED tier a live rule actually moved is marked
``tainted`` and never leaves the training export — a manual decision must not
come back as a learned label. Taint propagates through sticky, so the turn
after an overridden one is excluded too when it was held on that tier.

Design doc: docs/features/squilla-central-routing.md
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from services.squilla_central.policy import (
    PolicyRule,
    PolicySource,
    build_policy_source,
    clamp_candidates,
    select_rule,
    shift_by_weights,
)

TEXT_TIERS = ("c0", "c1", "c2", "c3")
ROUTE_CLASSES = ("R0", "R1", "R2", "R3")
# R0-R3 route classes map 1:1 onto the abstract c0-c3 tiers (OpenSquilla
# router_tiers.ROUTE_CLASS_TO_TIER). Kept local so the service has no import-time
# dependency on the opensquilla package (the V4 path imports it lazily).
ROUTE_CLASS_TO_TIER = dict(zip(ROUTE_CLASSES, TEXT_TIERS))

# ---------------------------------------------------------------------------
# Heuristic-fallback rule constants (OpenSquilla router.runtime.yaml /
# heuristic.py parity; keep in sync with extensions/squilla-router/router.ts,
# the plugin-side fallback). Used only when the V4 model is unavailable.
# ---------------------------------------------------------------------------

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

# Sticky defaults (OpenSquilla sticky_tier.max_user_len). Enabled by default:
# the client is pass-through, so the tier recorded here is exactly the tier
# served, and the prev-route accuracy concern that gated it off upstream does
# not apply.
STICKY_DEFAULT_MAX_USER_LEN = 200

RATINGS = {"up", "down", "neutral"}
MAX_MESSAGE_CHARS = 64_000
LIST_LIMIT_MAX = 100
EXPORT_LIMIT_MAX = 5_000

# OpenSquilla router_cfg.complaint_upgrade_max_chars default. A complaint is a
# SHORT reaction ("不对", "答非所问"); the cap stops a long prompt that merely
# quotes one of the terms from being read as dissatisfaction.
COMPLAINT_MAX_CHARS = 160

_complaint_terms_cache: tuple[str, ...] | None = None


def complaint_terms() -> tuple[str, ...]:
    """OpenSquilla's complaint term table, imported lazily.

    ``policy_data`` is a pure-data module, but the import stays lazy so this
    service remains importable (heuristic-only path, tests) on a host without
    the opensquilla package. An empty table silently disables the single most
    valuable training signal, so the failure is announced once.
    """
    global _complaint_terms_cache
    if _complaint_terms_cache is None:
        try:
            from opensquilla.engine.routing.policy_data import COMPLAINT_TERMS

            _complaint_terms_cache = tuple(COMPLAINT_TERMS)
        except Exception as exc:  # noqa: BLE001 - degrade, but loudly
            print(
                f"squilla-central: complaint terms unavailable ({exc}); "
                "captured turns will carry no correction signal"
            )
            _complaint_terms_cache = ()
    return _complaint_terms_cache


def detect_complaint(message: str) -> bool:
    """Did this turn complain about the previous answer? (OpenSquilla parity.)

    Recorded as a bare boolean: it is the label-alignment input that turns a
    stream of confirmations into actual corrections, and it derives from text
    the store never keeps.
    """
    text = message.strip()
    if len(text) > COMPLAINT_MAX_CHARS:
        return False
    lowered = text.lower()
    return any(term in lowered for term in complaint_terms())


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


def bypass_outcome(tier: str, band: str) -> dict[str, Any]:
    """Outcome shape for turns that skip the text classifier (e.g. image turns)."""
    return {
        "band": band,
        "base_tier": tier,
        "gated_tier": tier,
        "final_tier": tier,
        "confidence": 1.0,
        "margin": 0.0,
        "probabilities": {t: 0.0 for t in TEXT_TIERS},
        "flags": {},
        "flag_upgraded": False,
        "top_anchors": [],
        "embedding": None,
    }


def snap_to_available(tier: str, available: list[str]) -> str:
    """Snap a tier onto one the client can actually serve.

    Prefer the same tier, then walk UP so an unconfigured tier never silently
    downgrades a turn, then walk down. Client-side profiles may configure only a
    subset of c0-c3, so this runs centrally and the client does a pure lookup.
    """
    if not available:
        return tier
    start = TEXT_TIERS.index(tier) if tier in TEXT_TIERS else 1
    for candidate in TEXT_TIERS[start:]:
        if candidate in available:
            return candidate
    for candidate in reversed(TEXT_TIERS[:start]):
        if candidate in available:
            return candidate
    return available[0]


def apply_sticky(
    desired: str, last_tier: str | None, prompt_len: int, sticky: dict[str, Any]
) -> tuple[str, bool]:
    """KV-cache-aware sticky routing (OpenSquilla predictor.py _apply_sticky_tier).

    Switching models mid-session throws away the provider-side prompt cache, so a
    "cheaper" tier can cost MORE (re-paying the whole context uncached). On a
    short continuation turn, never route below the previous turn's tier. Only
    downgrades are blocked — an upgrade busts the cache too, but a genuinely
    harder turn is worth it.

    Runs centrally: the client is pass-through, so the tier returned here IS the
    served tier, which keeps the decision trail an accurate training label.
    """
    if not sticky.get("enabled", True) or not last_tier or last_tier not in TEXT_TIERS:
        return desired, False
    if prompt_len > int(sticky.get("maxUserLen", STICKY_DEFAULT_MAX_USER_LEN)):
        return desired, False
    if TEXT_TIERS.index(last_tier) <= TEXT_TIERS.index(desired):
        return desired, False
    return last_tier, True


@dataclass(frozen=True)
class TierPlan:
    """The tail of the decision: what gets served, and why it moved."""

    tier: str
    stuck: bool
    clamp_satisfiable: bool = True


def plan_tier(
    *,
    classifier_tier: str,
    probabilities: dict[str, float],
    available: list[str],
    rule: PolicyRule | None,
    last_tier: str | None,
    prompt_len: int,
    sticky: dict[str, Any],
) -> TierPlan:
    """Run the post-classifier chain: policy -> snap -> sticky.

    Called twice per turn — once with ``rule=None`` for the baseline the model
    alone would have produced, once with the matched rule. Comparing the two
    outcomes is what defines "this turn was intervened on"; comparing an
    intermediate tier would mislabel turns where the rule nudged something that
    snap or sticky then collapsed back.

    A rule's floor/ceiling narrows the candidate SET before snap and sticky, so
    neither can escape the bound: snap only picks from the band, and sticky can
    only hold a previous tier that has been pulled into it.
    """
    desired = classifier_tier
    candidates, satisfiable = list(available), True
    if rule is not None:
        desired = shift_by_weights(probabilities, desired, rule.action.weights)
        candidates, satisfiable = clamp_candidates(
            available, rule.action.floor, rule.action.ceiling
        )
    desired = snap_to_available(desired, candidates)
    previous = snap_to_available(last_tier, candidates) if last_tier else None
    served, stuck = apply_sticky(desired, previous, prompt_len, sticky)
    return TierPlan(tier=served, stuck=stuck, clamp_satisfiable=satisfiable)


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


class V4Classifier:
    """Central classifier backed by OpenSquilla's real V4 Phase 3 model.

    Runs the trained BGE(ONNX) + LightGBM + MLP ensemble through
    ``V4Phase3Strategy`` — the same inference core OpenSquilla uses in-process.
    The opensquilla import and the heavy ML deps / LFS bundle are only touched
    here (constructed at startup), so the module stays importable without them
    and ``main`` degrades to ``classify_heuristic`` when this cannot be built.

    Only the current turn is fed: the store holds no plaintext history, so V4's
    history channels (prev user/assistant text, route history) stay empty —
    first-turn feature quality. KV-cache stickiness is applied by the service.

    ``capture_features`` turns on the self-learning capture hook: the strategy
    then surfaces the exact 390-dim vector the heads consumed, which is the only
    way a retrain can reproduce this decision. ``capture_raw_bge`` adds the
    1536-dim raw embedding needed to retrain the MLP head (~4x the row size, so
    it is opt-in on top).
    """

    def __init__(
        self,
        bundle_dir: str | None = None,
        confidence_threshold: float = 0.5,
        *,
        capture_features: bool = True,
        capture_raw_bge: bool = False,
    ) -> None:
        from opensquilla.squilla_router.self_learning.schema import encode_features
        from opensquilla.squilla_router.v4_phase3 import V4Phase3Strategy

        self._encode_features = encode_features
        # require_router_runtime=True: raise on any load failure (missing deps,
        # LFS pointers, bad bundle) so the caller's try/except degrades cleanly
        # instead of silently serving the default tier every turn.
        self._strategy = V4Phase3Strategy(
            bundle_dir=bundle_dir,
            confidence_threshold=confidence_threshold,
            require_router_runtime=True,
            emit_train_features=capture_features,
            emit_raw_bge=capture_raw_bge,
        )

    def _training_capture(self, extra: dict[str, Any]) -> dict[str, Any]:
        """Encode the captured feature vectors for storage (float16 base64).

        ``feature_schema_version`` hashes the fitted projections, so a bundle
        upgrade produces a new version and the offline builder refuses to mix
        the two feature bases instead of training on a silently shifted space.
        """
        captured = extra.get("_train_features")
        if not isinstance(captured, dict) or captured.get("features_390") is None:
            return {}
        raw_bge = captured.get("raw_bge_1536")
        return {
            "features_b64": self._encode_features(captured["features_390"]),
            "raw_bge_b64": self._encode_features(raw_bge) if raw_bge is not None else None,
            "feature_schema_version": str(captured.get("feature_schema_version") or "unknown"),
        }

    def classify(self, message: str) -> dict[str, Any]:
        # The strategy's classify() is async by interface but does no real IO;
        # each request thread has no running loop, so asyncio.run is safe here.
        tier, confidence, _source, extra = asyncio.run(
            self._strategy.classify(message, list(TEXT_TIERS))
        )
        route_probs = extra.get("probabilities") or {}
        probabilities = {
            tier_name: float(route_probs.get(route_class, 0.0))
            for route_class, tier_name in ROUTE_CLASS_TO_TIER.items()
        }
        # V4 does its own gating/postprocess internally, so base/gated/final all
        # collapse to the returned tier; flag_upgraded/top_anchors don't apply.
        return {
            "band": "v4",
            "base_tier": tier,
            "gated_tier": tier,
            "final_tier": tier,
            "confidence": float(confidence),
            "margin": float(extra.get("margin", 0.0)),
            "probabilities": probabilities,
            "flags": dict(extra.get("flags") or {}),
            "flag_upgraded": False,
            "top_anchors": [],
            "embedding": None,
            "route_class": str(extra.get("route_class") or ""),
            "difficulty": float(extra.get("difficulty", 0.0)),
            **self._training_capture(extra),
        }


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
  classifier_tier VARCHAR(8) NOT NULL DEFAULT '',
  stuck TINYINT(1) NOT NULL DEFAULT 0,
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
  turn_index INT NOT NULL DEFAULT 0,
  route_class VARCHAR(8) NOT NULL DEFAULT '',
  complaint TINYINT(1) NOT NULL DEFAULT 0,
  feature_schema_version VARCHAR(64) NOT NULL DEFAULT '',
  features_b64 LONGTEXT NULL,
  raw_bge_b64 LONGTEXT NULL,
  bias_rule VARCHAR(64) NOT NULL DEFAULT '',
  bias_mode VARCHAR(16) NOT NULL DEFAULT '',
  baseline_tier VARCHAR(8) NOT NULL DEFAULT '',
  tainted TINYINT(1) NOT NULL DEFAULT 0,
  INDEX idx_decisions_session (tenant_id, session_key, ts_ms),
  INDEX idx_decisions_export (tenant_id, tainted, ts_ms)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS is a
# no-op on an existing table, so an upgraded deployment needs them added
# explicitly; every one carries a DEFAULT so old rows stay readable.
_DECISION_COLUMN_ADDITIONS = (
    ("turn_index", "INT NOT NULL DEFAULT 0"),
    ("route_class", "VARCHAR(8) NOT NULL DEFAULT ''"),
    ("complaint", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("feature_schema_version", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("features_b64", "LONGTEXT NULL"),
    ("raw_bge_b64", "LONGTEXT NULL"),
    ("bias_rule", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("bias_mode", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("baseline_tier", "VARCHAR(8) NOT NULL DEFAULT ''"),
    ("tainted", "TINYINT(1) NOT NULL DEFAULT 0"),
)
_DDL_FEEDBACK = """
CREATE TABLE IF NOT EXISTS feedback (
  decision_id VARCHAR(64) NOT NULL PRIMARY KEY,
  rating VARCHAR(16) NOT NULL,
  ts_ms BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_SUMMARY_COLUMNS = (
    "decision_id, tenant_id, session_key, profile, ts_ms, band, base_tier, gated_tier, "
    "final_tier, classifier_tier, stuck, confidence, margin, probabilities, flags, char_len, "
    "attachment_count, top_anchors, policy_version, latency_ms, turn_index, route_class, "
    "complaint, bias_rule, bias_mode, baseline_tier, tainted"
)
# Ordered to match RouterTrainSample's field names so an exported row feeds
# opensquilla's offline builder without a translation layer.
_EXPORT_COLUMNS = (
    "session_key, turn_index, ts_ms, feature_schema_version, features_b64, raw_bge_b64, "
    "route_class, final_tier, probabilities, margin, confidence, complaint, band, decision_id"
)
# Column list for insert_decision; keeping it explicit (rather than relying on
# table order) means an ALTER on an upgraded deployment cannot silently shift
# values into the wrong columns.
_INSERT_COLUMNS = (
    "decision_id, tenant_id, session_key, profile, ts_ms, band, base_tier, gated_tier, "
    "final_tier, classifier_tier, stuck, confidence, margin, probabilities, flags, char_len, "
    "attachment_count, top_anchors, policy_version, latency_ms, embedding, turn_index, "
    "route_class, complaint, feature_schema_version, features_b64, raw_bge_b64, bias_rule, "
    "bias_mode, baseline_tier, tainted"
)


_TIER_TO_ROUTE_CLASS = {tier: route for route, tier in ROUTE_CLASS_TO_TIER.items()}


def _train_sample(row: tuple) -> dict[str, Any]:
    """Map an ``_EXPORT_COLUMNS`` row onto RouterTrainSample's field names.

    Field-for-field so ``RouterTrainSample.from_json_dict`` consumes the output
    directly. Three fields are pinned rather than stored: this service applies
    no confidence gate or large-context floor (V4 does its own postprocess and
    surfaces no flag for either), and it never serves counterfactual tiers —
    deliberate bias is recorded as ``tainted`` and excluded above, which is the
    opposite of an exploration sample the aligner would keep.
    """
    probabilities = json.loads(row[8]) if row[8] else {}
    final_tier = str(row[7])
    return {
        "session_key": row[0],
        "turn_index": int(row[1]),
        "ts": datetime.fromtimestamp(int(row[2]) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feature_schema_version": row[3] or "unknown",
        "features_390_b64": row[4],
        "raw_bge_1536_b64": row[5],
        "route_class": row[6] or _TIER_TO_ROUTE_CLASS.get(final_tier, "R1"),
        # The served tier IS the label: the client applies it verbatim.
        "final_route_class": _TIER_TO_ROUTE_CLASS.get(final_tier, "R1"),
        "routed_tier": final_tier,
        "probabilities": [float(probabilities.get(tier, 0.0)) for tier in TEXT_TIERS],
        "margin": float(row[9]),
        "confidence": float(row[10]),
        "complaint_detected": bool(row[11]),
        "anti_downgrade_applied": False,
        "confidence_gate_applied": False,
        "large_context_floor_applied": False,
        "image_route": row[12] == "image",
        "exploration": False,
        "decision_id": row[13],
    }


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
                self._add_missing_columns(cursor)
            self._conn.commit()

    def _add_missing_columns(self, cursor) -> None:
        """Bring an already-created ``decisions`` table up to the current shape."""
        cursor.execute("SHOW COLUMNS FROM decisions")
        existing = {str(row[0]) for row in cursor.fetchall()}
        for column, definition in _DECISION_COLUMN_ADDITIONS:
            if column not in existing:
                cursor.execute(f"ALTER TABLE decisions ADD COLUMN {column} {definition}")

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
        values = (
            record["decisionId"],
            record["tenantId"],
            record["sessionKey"],
            record["profile"],
            record["tsMs"],
            record["band"],
            record["baseTier"],
            record["gatedTier"],
            record["finalTier"],
            record["classifierTier"],
            int(bool(record["stuck"])),
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
            record["turnIndex"],
            record["routeClass"],
            int(bool(record["complaint"])),
            record["featureSchemaVersion"],
            record["featuresB64"],
            record["rawBgeB64"],
            record["biasRule"],
            record["biasMode"],
            record["baselineTier"],
            int(bool(record["tainted"])),
        )
        with self._lock, self._cursor() as cursor:
            cursor.execute(
                f"INSERT INTO decisions ({_INSERT_COLUMNS}) VALUES ("
                + ", ".join(["%s"] * len(values))
                + ")",
                values,
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
            "classifierTier": row[9],
            "stuck": bool(row[10]),
            "confidence": row[11],
            "margin": row[12],
            "probabilities": json.loads(row[13]),
            "flags": json.loads(row[14]),
            "charLen": row[15],
            "attachmentCount": row[16],
            "topAnchors": json.loads(row[17]),
            "policyVersion": row[18],
            "latencyMs": row[19],
            "turnIndex": row[20],
            "routeClass": row[21],
            "complaint": bool(row[22]),
            "biasRule": row[23],
            "biasMode": row[24],
            "baselineTier": row[25],
            "tainted": bool(row[26]),
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

    def last_decision(self, tenant_id: str, session_key: str) -> dict[str, Any] | None:
        """The session's previous turn: sticky basis, taint chain, turn counter.

        Reads the decision trail rather than process memory so sticky stays
        correct across central instances and restarts (idx_decisions_session
        covers this exact lookup). One query serves all three needs, so the hot
        path keeps a single round trip.
        """
        if not session_key:
            return None
        with self._lock, self._cursor() as cursor:
            cursor.execute(
                "SELECT final_tier, tainted, turn_index FROM decisions "
                "WHERE tenant_id = %s AND session_key = %s ORDER BY ts_ms DESC LIMIT 1",
                (tenant_id, session_key),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {"tier": row[0], "tainted": bool(row[1]), "turnIndex": int(row[2])}

    def export_training_rows(
        self, tenant_id: str, since_ms: int, limit: int
    ) -> list[dict[str, Any]]:
        """Training rows for one tenant, in RouterTrainSample shape.

        ``tainted = 0`` is the hard discard: any turn whose served tier was
        moved by an operator bias rule — or held onto a biased tier by sticky —
        never leaves this query, so a manual override can never become a
        training label. ``features_b64 IS NOT NULL`` drops turns captured before
        the feature hook was on and turns that bypassed the classifier.
        """
        with self._lock, self._cursor() as cursor:
            cursor.execute(
                f"SELECT {_EXPORT_COLUMNS} FROM decisions "
                "WHERE tenant_id = %s AND tainted = 0 AND features_b64 IS NOT NULL "
                "AND ts_ms >= %s ORDER BY ts_ms ASC LIMIT %s",
                (tenant_id, since_ms, limit),
            )
            return [_train_sample(row) for row in cursor.fetchall()]

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
            # Corpus health: a dataset of pure confirmations trains nothing, so
            # `complaints` + rated rows are the numbers that say whether a
            # retrain is worth running at all.
            cursor.execute(
                "SELECT COUNT(*), SUM(features_b64 IS NOT NULL), SUM(tainted), SUM(complaint), "
                "SUM(features_b64 IS NOT NULL AND tainted = 0) "
                "FROM decisions WHERE tenant_id = %s",
                (tenant_id,),
            )
            total, captured, tainted, complaints, trainable = cursor.fetchone() or (0, 0, 0, 0, 0)
            # Per-rule impact: how often each rule fired, and how often it
            # actually moved the tier. A rule that fires constantly but never
            # changes anything is dead weight; one that changes everything is
            # probably too broad. Both are invisible without this breakdown.
            cursor.execute(
                "SELECT bias_rule, bias_mode, COUNT(*), SUM(final_tier <> baseline_tier) "
                "FROM decisions WHERE tenant_id = %s AND bias_rule <> '' "
                "GROUP BY bias_rule, bias_mode",
                (tenant_id,),
            )
            rule_rows = cursor.fetchall()
        return {
            "tiers": {row[0]: row[1] for row in tiers},
            "bands": {row[0]: row[1] for row in bands},
            "profiles": {row[0]: row[1] for row in profiles},
            "ratings": {row[0]: row[1] for row in ratings},
            "training": {
                "decisions": int(total or 0),
                "withFeatures": int(captured or 0),
                "tainted": int(tainted or 0),
                "complaints": int(complaints or 0),
                "trainable": int(trainable or 0),
            },
            "rules": [
                {
                    "rule": row[0],
                    "mode": row[1],
                    "matched": int(row[2] or 0),
                    "changed": int(row[3] or 0),
                }
                for row in rule_rows
            ],
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
        classifier: Any = None,  # V4Classifier (or a fake); None → heuristic fallback
        default_tier: str = "c1",
        sticky: dict[str, Any] | None = None,
        policy: PolicySource | None = None,
        policy_version: str = "central-py-v1",
        token: str | None = None,
        now_ms: Any = None,
    ) -> None:
        self.store = store
        # None means the V4 model wasn't available at startup; every turn then
        # routes through the dependency-free band heuristic.
        self.classifier = classifier
        # KV-cache sticky policy (moved here from the plugin, which is now
        # pass-through). tokenhub can version this alongside the other policy.
        self.sticky = sticky if sticky is not None else {"enabled": True}
        # Operator tier-probability overrides. Any turn one of these actually
        # moves is marked tainted and excluded from training export — a manual
        # decision must never come back as a learned label.
        self.policy = policy if policy is not None else PolicySource()
        self.default_tier = default_tier
        self.policy_version = policy_version
        self.token = token
        self.now_ms = now_ms if now_ms is not None else (lambda: int(time.time() * 1000))

    def handle(
        self, method: str, path: str, query: dict[str, list[str]], body: Any, auth: str | None
    ) -> tuple[int, dict[str, Any]]:
        if path == "/healthz" and method == "GET":
            return 200, {
                "status": "ok",
                "policyVersion": self.policy_version,
                "classifier": "v4" if self.classifier is not None else "heuristic",
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
        if path == "/v1/train/export" and method == "GET":
            return self._export(query)
        if path == "/v1/policy" and method == "GET":
            # What is live right now, including rules rejected at load. Ops must
            # be able to confirm the running policy without shelling into a box.
            return 200, self.policy.snapshot.summary()
        if path == "/v1/policy/reload" and method == "POST":
            # Manual reload for "I just pushed the file and want it now"; the
            # background watcher would pick it up anyway.
            self.policy.reload()
            return 200, self.policy.snapshot.summary()
        if path == "/v1/policy/simulate" and method == "POST":
            return self._simulate(body)
        return 404, {"error": "not found"}

    def _simulate(self, body: Any) -> tuple[int, dict[str, Any]]:
        """Answer "which rule would fire, and what would it do?" without routing.

        The safety net for an ops surface: a floor/ceiling/pin is a blunt
        instrument, and an operator needs to check a rule against a concrete
        tenant/profile/time before it reaches real traffic — including rules
        that are still disabled or dry-run.
        """
        if not isinstance(body, dict):
            return 400, {"error": "body must be a JSON object"}
        tenant_id = body.get("tenantId")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            return 400, {"error": "tenantId is required"}
        tier = body.get("classifierTier") if body.get("classifierTier") in TEXT_TIERS else "c1"
        raw_available = body.get("availableTiers")
        available = (
            [t for t in raw_available if t in TEXT_TIERS]
            if isinstance(raw_available, list)
            else list(TEXT_TIERS)
        ) or list(TEXT_TIERS)
        at_ms = body.get("atMs") if isinstance(body.get("atMs"), (int, float)) else self.now_ms()
        probabilities = (
            body.get("probabilities")
            if isinstance(body.get("probabilities"), dict)
            else {t: (1.0 if t == tier else 0.0) for t in TEXT_TIERS}
        )

        rule = select_rule(
            self.policy.snapshot.rules,
            tenant_id=tenant_id,
            profile=str(body.get("profile") or ""),
            session_key=str(body.get("sessionKey") or ""),
            now_ms=int(at_ms),
        )
        shared = {
            "classifier_tier": tier,
            "probabilities": probabilities,
            "available": available,
            "last_tier": body.get("lastTier") if body.get("lastTier") in TEXT_TIERS else None,
            "prompt_len": int(body.get("charLen") or 0),
            "sticky": self.sticky,
        }
        baseline = plan_tier(rule=None, **shared)
        proposed = plan_tier(rule=rule, **shared) if rule is not None else baseline
        return 200, {
            "rule": rule.summary() if rule is not None else None,
            "baselineTier": baseline.tier,
            "proposedTier": proposed.tier,
            "servedTier": baseline.tier if (rule and rule.dry_run) else proposed.tier,
            "changed": proposed.tier != baseline.tier,
            "clampUnsatisfiable": not proposed.clamp_satisfiable,
        }

    def _export(self, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        """Training corpus for one tenant, already stripped of biased turns.

        Rows come out in RouterTrainSample field shape, so the offline builder
        consumes them directly. The taint filter lives in the store query rather
        than here: exclusion is a property of the data, not of this endpoint,
        and any future reader gets it for free.
        """
        tenant_id = (query.get("tenantId") or [None])[0]
        if not tenant_id:
            return 400, {"error": "tenantId is required"}
        try:
            since_ms = int((query.get("sinceMs") or ["0"])[0])
        except ValueError:
            since_ms = 0
        try:
            limit = int((query.get("limit") or ["1000"])[0])
        except ValueError:
            limit = 1000
        limit = max(1, min(limit, EXPORT_LIMIT_MAX))
        samples = self.store.export_training_rows(tenant_id, max(since_ms, 0), limit)
        return 200, {"samples": samples, "count": len(samples)}

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
        has_image = body.get("hasImage") is True
        # Tiers the calling profile can actually serve. Absent -> assume all, so
        # a client that omits it still gets a valid tier.
        raw_available = body.get("availableTiers")
        available = (
            [t for t in raw_available if t in TEXT_TIERS]
            if isinstance(raw_available, list)
            else list(TEXT_TIERS)
        ) or list(TEXT_TIERS)

        started = self.now_ms()
        # Image turns bypass the text classifier: text complexity says nothing
        # about vision needs, so serve the strongest available tier (most likely
        # to be vision-capable).
        if has_image:
            outcome = bypass_outcome(available[-1], "image")
        elif self.classifier is not None:
            outcome = self.classifier.classify(message)
        else:
            # Central-side band heuristic, so clients only serve their own
            # defaultTier when THIS service is unreachable.
            outcome = classify_heuristic(message, attachment_count)

        # Client is pass-through, so every remaining judgment happens here:
        # operator policy -> snap onto a servable tier -> KV-cache sticky against
        # the tier this session was actually served last turn.
        classifier_tier = outcome["final_tier"]
        previous = self.store.last_decision(tenant_id, session_key)
        last_tier = previous["tier"] if previous else None
        # Image turns never reach the classifier, so there is no model judgment
        # for a rule to adjust; leaving them out keeps "vision needs vision" a
        # property of the service rather than something a rule can break.
        rule = (
            None
            if has_image
            else select_rule(
                self.policy.snapshot.rules,
                tenant_id=tenant_id,
                profile=profile,
                session_key=session_key,
                now_ms=started,
            )
        )

        def run(active: PolicyRule | None) -> TierPlan:
            return plan_tier(
                classifier_tier=classifier_tier,
                probabilities=outcome["probabilities"],
                available=available,
                rule=active,
                last_tier=last_tier,
                prompt_len=len(message),
                sticky=self.sticky,
            )

        # Always compute what the model alone would have served: it is both the
        # dry-run answer and the reference that defines whether this turn was
        # actually intervened on.
        baseline = run(None)
        proposed = run(rule) if rule is not None else baseline
        dry_run = rule is not None and rule.dry_run
        plan = baseline if dry_run else proposed

        # Taint marks a turn whose SERVED tier reflects an operator decision
        # rather than the model's — compared on the outcome, not an intermediate,
        # so a nudge that snap or sticky collapsed back is still trainable. It
        # propagates through sticky: a turn held on an overridden tier was not
        # chosen by the model either, and training on it would launder the
        # override back in as a label one turn later.
        tainted = plan.tier != baseline.tier or bool(
            plan.stuck and previous is not None and previous["tainted"]
        )
        bias_mode = ("dry_run" if dry_run else "applied") if rule is not None else ""
        served, stuck = plan.tier, plan.stuck
        capture = outcome.get("features_b64")

        record = {
            "decisionId": str(uuid.uuid4()),
            "tenantId": tenant_id,
            "sessionKey": session_key,
            "profile": profile,
            "tsMs": started,
            "band": outcome["band"],
            "baseTier": outcome["base_tier"],
            "gatedTier": outcome["gated_tier"],
            # finalTier is the tier actually SERVED (after snap + sticky). The
            # client applies it verbatim, so this column is a truthful training
            # label; classifierTier keeps the model's own pick for diagnostics.
            "finalTier": served,
            "classifierTier": classifier_tier,
            "stuck": stuck,
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
            # Self-learning capture. turnIndex + complaint are the label
            # alignment inputs; features/schema version are what a retrain
            # actually consumes; biasRule + tainted are the discard controls.
            "turnIndex": (previous["turnIndex"] + 1) if previous else 0,
            "routeClass": outcome.get("route_class") or "",
            "complaint": detect_complaint(message),
            "featureSchemaVersion": outcome.get("feature_schema_version") or "",
            "featuresB64": capture,
            "rawBgeB64": outcome.get("raw_bge_b64"),
            "biasRule": rule.name if rule is not None else "",
            "biasMode": bias_mode,
            # What the model alone would have served. Makes a rule's real impact
            # measurable (including in dry run, where it IS the served tier).
            "baselineTier": baseline.tier,
            "tainted": tainted,
        }
        self.store.insert_decision(record)
        # Generic wire response: `tier` (the abstract c0-c3 capability tier the
        # client maps to a model) + confidence are the only contract; everything
        # algorithm-specific goes under opaque `meta`, which the client logs but
        # never branches on. Swapping the classifier keeps this shape unchanged.
        # `tier` is the SERVED tier — the client applies it verbatim.
        return 200, {
            "decisionId": record["decisionId"],
            "tier": served,
            "confidence": outcome["confidence"],
            "policyVersion": self.policy_version,
            "meta": {
                "routeClass": outcome.get("route_class")
                or ROUTE_CLASSES[TEXT_TIERS.index(served)],
                "band": outcome["band"],
                "flags": outcome["flags"],
                "flagUpgraded": outcome["flag_upgraded"],
                "margin": outcome["margin"],
                "difficulty": outcome.get("difficulty", 0.0),
                # What the classifier picked before snap/sticky, so a surprising
                # served tier is explainable straight from the response.
                "classifierTier": classifier_tier,
                "stuck": stuck,
                # Named so an operator can see WHICH rule touched a turn, in
                # which mode, and what it would have been otherwise — without
                # opening the store. Empty when no rule matched.
                "biasRule": rule.name if rule is not None else "",
                "biasMode": bias_mode,
                "baselineTier": baseline.tier,
                # A floor/ceiling the client's tier table cannot satisfy: the
                # rule is impossible for this profile and is being ignored.
                "clampUnsatisfiable": not proposed.clamp_satisfiable,
                "tainted": tainted,
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


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SquillaRouter central routing service")
    parser.add_argument("--host", default=os.environ.get("SQUILLA_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SQUILLA_PORT", "8710")))
    args = parser.parse_args(argv)

    default_tier = os.environ.get("SQUILLA_DEFAULT_TIER", "c1")
    policy = build_policy_source()
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
        classifier=_build_classifier(),
        default_tier=default_tier if default_tier in TEXT_TIERS else "c1",
        sticky={
            "enabled": _env_flag("SQUILLA_STICKY", True),
            "maxUserLen": int(
                os.environ.get("SQUILLA_STICKY_MAX_USER_LEN", STICKY_DEFAULT_MAX_USER_LEN)
            ),
        },
        policy=policy,
        policy_version=os.environ.get("SQUILLA_POLICY_VERSION", "central-py-v1"),
        token=os.environ.get("SQUILLA_CENTRAL_TOKEN") or None,
    )
    # Start the watcher only once the service is otherwise built, so a policy
    # file problem surfaces against a running server instead of at import time.
    policy.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(central))
    print(f"squilla-central listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    finally:
        policy.stop()


def _build_classifier() -> V4Classifier | None:
    """Build the real V4 classifier, or return None to run on the heuristic.

    ``SQUILLA_V4=0`` forces the heuristic (no ML deps / bundle needed). Any load
    failure (missing deps, unpulled LFS bundle, bad artifacts) is logged and
    degrades to the heuristic rather than crashing the service.
    """
    if os.environ.get("SQUILLA_V4", "1").lower() not in ("1", "true", "yes"):
        print("squilla-central: SQUILLA_V4 disabled; using heuristic classifier")
        return None
    try:
        return V4Classifier(
            bundle_dir=os.environ.get("SQUILLA_V4_BUNDLE_DIR") or None,
            confidence_threshold=float(os.environ.get("SQUILLA_CONFIDENCE_THRESHOLD", "0.5")),
            capture_features=_env_flag("SQUILLA_CAPTURE_FEATURES", True),
            # ~4x the row size; only needed to retrain the MLP head.
            capture_raw_bge=_env_flag("SQUILLA_CAPTURE_RAW_BGE", False),
        )
    except Exception as exc:  # noqa: BLE001 - degrade on any load failure
        print(f"squilla-central: V4 model unavailable ({exc}); using heuristic classifier")
        return None


if __name__ == "__main__":
    main()
