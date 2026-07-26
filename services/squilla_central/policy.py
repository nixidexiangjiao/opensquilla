"""Routing policy: the operator control surface over the model's tier choice.

The V4 classifier decides what a turn *needs*. This module is where a human
decides what the business *wants* — cost ceilings during off-peak, a quality
floor for a paying tenant, a hard pin while a provider is degraded, a temporary
nudge during a campaign. It is deliberately separate from ``server.py`` so the
control surface can be read, reviewed, and tested as one thing.

Design rules that shaped it:

* **Three action kinds, one canonical form.** ``weights`` nudges the
  probabilities (soft); ``floor``/``ceiling`` bound the served tier (hard).
  ``pin`` is config sugar for ``floor == ceiling`` and normalizes away at parse
  time, so the runtime has a single path.
* **Hard bounds are enforced structurally**, by narrowing the candidate tier
  set before snapping and sticky run — not by checking afterwards. Otherwise a
  snap-up or a sticky hold silently escapes the operator's ceiling.
* **Every rule can expire.** ``notAfter`` exists because temporary rules become
  permanent by being forgotten; an ops surface without expiry accumulates
  sediment.
* **Every rule can be shadowed.** ``dryRun`` records what the rule *would* have
  done without doing it, so impact is measurable before a rule goes live.
* **Bad config never takes routing down.** Parsing collects errors and skips
  the offending rules; a reload that fails keeps the last-known-good snapshot.

Nothing here does IO on the request path: ``PolicySource`` owns a single-slot
snapshot that a background thread swaps, and the hot path only reads it.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TEXT_TIERS = ("c0", "c1", "c2", "c3")

# Ratio buckets are integers so the same session always lands in the same
# bucket regardless of float formatting.
_RATIO_BUCKETS = 10_000
DEFAULT_RELOAD_SECONDS = 30.0


@dataclass(frozen=True)
class RuleScope:
    """Which turns a rule applies to. Every dimension is ANDed.

    An empty/None dimension means "any", so a rule with no scope at all is a
    global rule. ``ratio`` buckets deterministically on the session key rather
    than sampling per turn: a session that opts in must stay opted in, or the
    tier would flap mid-conversation and throw away the KV cache on every turn.
    """

    tenants: frozenset[str] = frozenset()
    profiles: frozenset[str] = frozenset()
    hours: tuple[int, int] | None = None
    not_before_ms: int | None = None
    not_after_ms: int | None = None
    ratio: float = 1.0

    def matches(self, *, tenant_id: str, profile: str, session_key: str, now_ms: int) -> bool:
        if self.tenants and tenant_id not in self.tenants:
            return False
        if self.profiles and profile not in self.profiles:
            return False
        if self.not_before_ms is not None and now_ms < self.not_before_ms:
            return False
        if self.not_after_ms is not None and now_ms >= self.not_after_ms:
            return False
        if self.hours is not None:
            hour = datetime.fromtimestamp(now_ms / 1000, tz=UTC).hour
            start, end = self.hours
            # A window may wrap midnight (22->6), so the wrapped case is a union.
            in_window = start <= hour < end if start < end else (hour >= start or hour < end)
            if not in_window:
                return False
        return self.ratio >= 1.0 or _in_ratio(session_key, self.ratio)


def _in_ratio(session_key: str, ratio: float) -> bool:
    """Stable per-session bucketing. Turns with no session key share one bucket."""
    if ratio <= 0:
        return False
    digest = hashlib.sha256(session_key.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % _RATIO_BUCKETS
    return bucket < int(ratio * _RATIO_BUCKETS)


@dataclass(frozen=True)
class RuleAction:
    """What the rule does. ``weights`` is a nudge; floor/ceiling are hard bounds."""

    weights: dict[str, float] = field(default_factory=dict)
    floor: str | None = None
    ceiling: str | None = None

    @property
    def pins(self) -> bool:
        """A pin is floor == ceiling — the tier is fixed regardless of the model."""
        return self.floor is not None and self.floor == self.ceiling

    def describe(self) -> str:
        """One-line summary for /v1/policy, so ops can read the live rule set."""
        if self.pins:
            return f"pin={self.floor}"
        parts = []
        if self.floor:
            parts.append(f"floor={self.floor}")
        if self.ceiling:
            parts.append(f"ceiling={self.ceiling}")
        if self.weights:
            terms = ",".join(f"{k}x{v:g}" for k, v in sorted(self.weights.items()))
            parts.append(f"weights={terms}")
        return " ".join(parts) or "noop"


@dataclass(frozen=True)
class PolicyRule:
    name: str
    action: RuleAction
    scope: RuleScope = field(default_factory=RuleScope)
    priority: int = 0
    enabled: bool = True
    dry_run: bool = False
    note: str = ""

    def matches(self, *, tenant_id: str, profile: str, session_key: str, now_ms: int) -> bool:
        return self.enabled and self.scope.matches(
            tenant_id=tenant_id, profile=profile, session_key=session_key, now_ms=now_ms
        )

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "priority": self.priority,
            "enabled": self.enabled,
            "dryRun": self.dry_run,
            "action": self.action.describe(),
            "scope": {
                "tenants": sorted(self.scope.tenants),
                "profiles": sorted(self.scope.profiles),
                "hours": list(self.scope.hours) if self.scope.hours else None,
                "notBefore": _iso(self.scope.not_before_ms),
                "notAfter": _iso(self.scope.not_after_ms),
                "ratio": self.scope.ratio,
            },
            "note": self.note,
        }


def _iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def select_rule(
    rules: list[PolicyRule], *, tenant_id: str, profile: str, session_key: str, now_ms: int
) -> PolicyRule | None:
    """Highest priority matching rule wins; ties break on list order.

    Priority rather than bare list order because an ops surface gets appended
    to: a new narrow rule must be able to outrank a broad one without anyone
    having to re-sort the file.
    """
    best: PolicyRule | None = None
    for rule in rules:
        if not rule.matches(
            tenant_id=tenant_id, profile=profile, session_key=session_key, now_ms=now_ms
        ):
            continue
        if best is None or rule.priority > best.priority:
            best = rule
    return best


def clamp_candidates(
    available: list[str], floor: str | None, ceiling: str | None
) -> tuple[list[str], bool]:
    """Narrow the servable tiers to the rule's [floor, ceiling] band.

    Returns ``(candidates, satisfiable)``. Narrowing the SET (rather than
    checking the tier afterwards) is what makes the bound survive the snap and
    sticky steps that run later — both of them can only pick from this list.

    When the band and the client's tier table do not intersect, the bound is
    impossible for this profile: routing must not fail, so the full set comes
    back with ``satisfiable=False`` for the operator to see in the trail.
    """
    low = TEXT_TIERS.index(floor) if floor in TEXT_TIERS else 0
    high = TEXT_TIERS.index(ceiling) if ceiling in TEXT_TIERS else len(TEXT_TIERS) - 1
    candidates = [t for t in available if low <= TEXT_TIERS.index(t) <= high]
    return (candidates, True) if candidates else (list(available), False)


def shift_by_weights(
    probabilities: dict[str, float], classifier_tier: str, weights: dict[str, float]
) -> str:
    """Move the classifier's tier by how far the weights move the argmax.

    A DELTA, not an absolute re-selection: V4's postprocess (margin upgrade,
    under-routing safety net) often lands above its own argmax, and re-deriving
    the tier from weighted probabilities would silently discard those
    corrections on every turn the rule touches — including the ones the
    operator only meant to nudge.
    """
    if classifier_tier not in TEXT_TIERS or not weights:
        return classifier_tier
    ranked = [float(probabilities.get(tier, 0.0)) for tier in TEXT_TIERS]
    if not any(ranked):
        return classifier_tier  # bypass turns carry no distribution to weight
    weighted = [p * weights.get(tier, 1.0) for p, tier in zip(ranked, TEXT_TIERS)]
    shift = weighted.index(max(weighted)) - ranked.index(max(ranked))
    if shift == 0:
        return classifier_tier
    moved = min(max(TEXT_TIERS.index(classifier_tier) + shift, 0), len(TEXT_TIERS) - 1)
    return TEXT_TIERS[moved]


# ---------------------------------------------------------------------------
# Parsing (tolerant: bad rules are dropped and reported, never fatal)
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    rules: list[PolicyRule]
    errors: list[str]


def parse_rules(payload: Any) -> ParseResult:
    """Parse the rule list. Collects errors instead of raising.

    A typo in one rule must not take the whole policy — or routing — down, so
    every rejected rule is reported by index/name and the rest still load.
    """
    if payload is None:
        return ParseResult([], [])
    if isinstance(payload, dict):  # allow {"rules": [...]} for a versioned file
        payload = payload.get("rules")
    if not isinstance(payload, list):
        return ParseResult([], ["policy must be a JSON list of rules"])

    rules: list[PolicyRule] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(payload):
        name = ""
        if isinstance(entry, dict):
            name = str(entry.get("name") or f"rule-{index}")
        try:
            rule = _parse_rule(entry, index)
        except ValueError as exc:
            errors.append(f"{name or f'rule-{index}'}: {exc}")
            continue
        if rule.name in seen:
            errors.append(f"{rule.name}: duplicate rule name; keeping the first")
            continue
        seen.add(rule.name)
        rules.append(rule)
    return ParseResult(rules, errors)


def _parse_rule(entry: Any, index: int) -> PolicyRule:
    if not isinstance(entry, dict):
        raise ValueError("rule must be an object")
    name = str(entry.get("name") or f"rule-{index}")
    action = _parse_action(entry)
    return PolicyRule(
        name=name,
        action=action,
        scope=_parse_scope(entry),
        priority=_parse_int(entry.get("priority"), default=0, label="priority"),
        enabled=entry.get("enabled") is not False,
        dry_run=entry.get("dryRun") is True,
        note=str(entry.get("note") or ""),
    )


def _parse_action(entry: dict[str, Any]) -> RuleAction:
    pin = _parse_tier(entry.get("pin"), "pin")
    floor = _parse_tier(entry.get("floor"), "floor")
    ceiling = _parse_tier(entry.get("ceiling"), "ceiling")
    if pin is not None:
        if floor is not None or ceiling is not None:
            raise ValueError("pin cannot be combined with floor/ceiling")
        # Sugar only: a pin IS a degenerate band, so the runtime sees one shape.
        floor = ceiling = pin
    if floor and ceiling and TEXT_TIERS.index(floor) > TEXT_TIERS.index(ceiling):
        raise ValueError(f"floor {floor} is above ceiling {ceiling}")

    weights: dict[str, float] = {}
    raw_weights = entry.get("weights")
    if raw_weights is not None:
        if not isinstance(raw_weights, dict):
            raise ValueError("weights must be an object")
        for tier, value in raw_weights.items():
            if tier not in TEXT_TIERS:
                raise ValueError(f"unknown tier {tier!r} in weights")
            if not isinstance(value, (int, float)) or float(value) <= 0:
                raise ValueError(f"weight for {tier} must be a positive number")
            weights[tier] = float(value)

    if not weights and floor is None and ceiling is None:
        raise ValueError("rule does nothing: needs weights, floor, ceiling, or pin")
    return RuleAction(weights=weights, floor=floor, ceiling=ceiling)


def _parse_scope(entry: dict[str, Any]) -> RuleScope:
    ratio = entry.get("ratio")
    if ratio is None:
        ratio_value = 1.0
    elif isinstance(ratio, (int, float)) and 0.0 <= float(ratio) <= 1.0:
        ratio_value = float(ratio)
    else:
        raise ValueError("ratio must be a number between 0 and 1")
    return RuleScope(
        tenants=_parse_scope_set(entry.get("tenants"), "tenants"),
        profiles=_parse_scope_set(entry.get("profiles"), "profiles"),
        hours=_parse_hours(entry.get("hours")),
        not_before_ms=_parse_timestamp(entry.get("notBefore"), "notBefore"),
        not_after_ms=_parse_timestamp(entry.get("notAfter"), "notAfter"),
        ratio=ratio_value,
    )


def _parse_scope_set(raw: Any, label: str) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list")
    return frozenset(str(item) for item in raw)


def _parse_tier(raw: Any, label: str) -> str | None:
    if raw is None:
        return None
    if raw not in TEXT_TIERS:
        raise ValueError(f"{label} must be one of {', '.join(TEXT_TIERS)}")
    return str(raw)


def _parse_int(raw: Any, *, default: int, label: str) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{label} must be a number")
    return int(raw)


def _parse_hours(raw: Any) -> tuple[int, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError("hours must be a [start, end] pair")
    try:
        start, end = int(raw[0]) % 24, int(raw[1]) % 24
    except (TypeError, ValueError) as exc:
        raise ValueError("hours must contain two integers") from exc
    if start == end:
        raise ValueError("hours start and end must differ (use no window for all day)")
    return (start, end)


def _parse_timestamp(raw: Any, label: str) -> int | None:
    """Accept an ISO8601 instant or epoch millis; store millis."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    if not isinstance(raw, str):
        raise ValueError(f"{label} must be an ISO8601 string or epoch millis")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid ISO8601 instant") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Source: single-slot snapshot, background reload, fail-open
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicySnapshot:
    rules: list[PolicyRule]
    errors: list[str]
    source: str
    loaded_at_ms: int
    version: str

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "loadedAtMs": self.loaded_at_ms,
            "ruleCount": len(self.rules),
            "errors": list(self.errors),
            "rules": [rule.summary() for rule in self.rules],
        }


EMPTY_SNAPSHOT = PolicySnapshot(rules=[], errors=[], source="none", loaded_at_ms=0, version="empty")


class PolicySource:
    """Holds the live rule set; reloads a policy file in the background.

    The request path only reads ``.snapshot`` (a single attribute read), so a
    reload can never add latency or a partially-applied rule set to a turn: the
    new snapshot is built off-thread and swapped in one assignment.

    Fail-open is deliberate. A policy file that goes missing or stops parsing
    keeps the last-known-good rules and records the error for ``/v1/policy``,
    because losing the ceiling that caps spend is worse than running a slightly
    stale one, and an unreadable file is usually a deploy glitch.
    """

    def __init__(
        self,
        *,
        inline: str | None = None,
        path: str | None = None,
        reload_seconds: float = DEFAULT_RELOAD_SECONDS,
        now_ms: Any = None,
        log: Any = print,
    ) -> None:
        self._path = Path(path) if path else None
        self._reload_seconds = reload_seconds
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mtime: float | None = None
        self.snapshot: PolicySnapshot = EMPTY_SNAPSHOT
        if self._path is not None:
            self.reload()
        elif inline:
            self._apply(parse_rules(_load_json(inline)), source="env", version=_digest(inline))

    def reload(self) -> bool:
        """Re-read the policy file. Returns True when the snapshot changed."""
        if self._path is None:
            return False
        try:
            raw = self._path.read_text(encoding="utf-8")
            mtime = self._path.stat().st_mtime
        except OSError as exc:
            self._keep_last_known_good(f"cannot read {self._path}: {exc}")
            return False
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            self._keep_last_known_good(f"{self._path} is not valid JSON: {exc}")
            return False
        self._mtime = mtime
        self._apply(parse_rules(payload), source=str(self._path), version=_digest(raw))
        return True

    def _apply(self, parsed: ParseResult, *, source: str, version: str) -> None:
        if version == self.snapshot.version and source == self.snapshot.source:
            return
        for error in parsed.errors:
            self._log(f"squilla-central: policy rule rejected — {error}")
        self.snapshot = PolicySnapshot(
            rules=parsed.rules,
            errors=parsed.errors,
            source=source,
            loaded_at_ms=self._now_ms(),
            version=version,
        )
        self._log(
            f"squilla-central: policy loaded from {source} "
            f"({len(parsed.rules)} rules, {len(parsed.errors)} rejected)"
        )

    def _keep_last_known_good(self, message: str) -> None:
        self._log(f"squilla-central: {message}; keeping last-known-good policy")

    def start(self) -> None:
        if self._path is None or self._thread is not None or self._reload_seconds <= 0:
            return
        self._thread = threading.Thread(target=self._watch, name="policy-reload", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._reload_seconds + 1)
            self._thread = None

    def _watch(self) -> None:
        while not self._stop.wait(self._reload_seconds):
            try:
                if self._path is not None and self._path.stat().st_mtime != self._mtime:
                    self.reload()
            except OSError as exc:
                self._keep_last_known_good(f"policy stat failed: {exc}")


def _load_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def build_policy_source(env: Any = None, log: Any = print) -> PolicySource:
    """Wire the policy source from the environment.

    ``SQUILLA_POLICY_FILE`` is the managed path (hot-reloaded);
    ``SQUILLA_TIER_BIAS`` stays as the inline bootstrap for small/static setups
    and single-container deploys where mounting a file is overkill.
    """
    environ = env if env is not None else os.environ
    return PolicySource(
        inline=environ.get("SQUILLA_TIER_BIAS"),
        path=environ.get("SQUILLA_POLICY_FILE") or None,
        reload_seconds=float(environ.get("SQUILLA_POLICY_RELOAD_SECONDS", DEFAULT_RELOAD_SECONDS)),
        log=log,
    )
