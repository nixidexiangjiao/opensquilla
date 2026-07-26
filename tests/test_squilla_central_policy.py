"""Tests for the operator control surface (services/squilla_central/policy.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from services.squilla_central.policy import (
    DEFAULT_RELOAD_SECONDS,
    PolicyRule,
    PolicySource,
    RuleAction,
    RuleScope,
    build_policy_source,
    clamp_candidates,
    parse_rules,
    select_rule,
    shift_by_weights,
)

TIERS = ["c0", "c1", "c2", "c3"]
# 2024-01-01T09:00:00Z — a fixed instant so hour-window tests read literally.
NINE_AM = int(datetime(2024, 1, 1, 9, 0, tzinfo=UTC).timestamp() * 1000)
HOUR = 3_600_000


def at(hours: float) -> int:
    return int(NINE_AM + hours * HOUR)


# --- Parsing ---------------------------------------------------------------


def test_parses_the_three_action_kinds():
    parsed = parse_rules(
        [
            {"name": "nudge", "weights": {"c3": 2.0}},
            {"name": "cap", "ceiling": "c1"},
            {"name": "guarantee", "floor": "c2"},
            {"name": "band", "floor": "c1", "ceiling": "c2"},
        ]
    )
    assert parsed.errors == []
    assert [r.action.describe() for r in parsed.rules] == [
        "weights=c3x2",
        "ceiling=c1",
        "floor=c2",
        "floor=c1 ceiling=c2",
    ]


def test_pin_is_sugar_for_a_degenerate_band():
    rule = parse_rules([{"name": "freeze", "pin": "c1"}]).rules[0]
    # One runtime shape: a pin IS floor == ceiling, so nothing downstream needs
    # to know pins exist.
    assert rule.action.floor == "c1" and rule.action.ceiling == "c1"
    assert rule.action.pins is True
    assert rule.action.describe() == "pin=c1"


def test_rejects_contradictory_or_empty_rules_without_dropping_the_rest():
    parsed = parse_rules(
        [
            {"name": "inverted", "floor": "c3", "ceiling": "c0"},
            {"name": "both", "pin": "c1", "ceiling": "c2"},
            {"name": "noop"},
            {"name": "bad-tier", "pin": "c9"},
            {"name": "bad-weight", "weights": {"c3": -1}},
            {"name": "ok", "ceiling": "c2"},
        ]
    )
    assert [r.name for r in parsed.rules] == ["ok"]
    assert [e.split(":")[0] for e in parsed.errors] == [
        "inverted", "both", "noop", "bad-tier", "bad-weight",
    ]


def test_rejects_duplicate_names_keeping_the_first():
    parsed = parse_rules(
        [{"name": "dup", "ceiling": "c1"}, {"name": "dup", "ceiling": "c3"}]
    )
    assert len(parsed.rules) == 1
    assert parsed.rules[0].action.ceiling == "c1"
    assert "duplicate" in parsed.errors[0]


def test_accepts_a_versioned_object_wrapper_and_rejects_junk():
    assert len(parse_rules({"rules": [{"name": "a", "pin": "c1"}]}).rules) == 1
    assert parse_rules(None).rules == []
    assert parse_rules("nope").errors == ["policy must be a JSON list of rules"]


def test_parses_validity_window_from_iso_or_millis():
    parsed = parse_rules(
        [
            {
                "name": "campaign",
                "floor": "c2",
                "notBefore": "2024-01-01T00:00:00Z",
                "notAfter": 1_704_153_600_000,
            }
        ]
    )
    rule = parsed.rules[0]
    assert rule.scope.not_before_ms == 1_704_067_200_000
    assert rule.scope.not_after_ms == 1_704_153_600_000
    assert parse_rules([{"name": "x", "pin": "c1", "notAfter": "soon"}]).errors


# --- Scope -----------------------------------------------------------------


def test_scope_dimensions_are_anded():
    scope = RuleScope(
        tenants=frozenset({"team-a"}), profiles=frozenset({"squilla/auto"}), hours=(9, 18)
    )
    ok = {"tenant_id": "team-a", "profile": "squilla/auto", "session_key": "s", "now_ms": at(0)}
    assert scope.matches(**ok) is True
    assert scope.matches(**{**ok, "tenant_id": "team-b"}) is False
    assert scope.matches(**{**ok, "profile": "other"}) is False
    assert scope.matches(**{**ok, "now_ms": at(11)}) is False


def test_hour_window_wraps_midnight():
    night = RuleScope(hours=(22, 6))
    base = {"tenant_id": "t", "profile": "p", "session_key": "s"}
    assert night.matches(**base, now_ms=at(14)) is True  # 23:00
    assert night.matches(**base, now_ms=at(-6)) is True  # 03:00
    assert night.matches(**base, now_ms=at(3)) is False  # 12:00


def test_validity_window_expires_the_rule():
    scope = RuleScope(not_before_ms=at(0), not_after_ms=at(2))
    base = {"tenant_id": "t", "profile": "p", "session_key": "s"}
    assert scope.matches(**base, now_ms=at(-1)) is False  # not started
    assert scope.matches(**base, now_ms=at(1)) is True
    # notAfter is exclusive: a rule stops the instant it expires, so a
    # forgotten temporary rule cannot keep running.
    assert scope.matches(**base, now_ms=at(2)) is False


def test_ratio_buckets_are_stable_per_session_and_roughly_proportional():
    scope = RuleScope(ratio=0.3)
    base = {"tenant_id": "t", "profile": "p", "now_ms": at(0)}
    # Same session always gets the same answer — a session must not flap
    # between tiers mid-conversation.
    verdicts = {scope.matches(**base, session_key="session-42") for _ in range(5)}
    assert len(verdicts) == 1

    sampled = sum(scope.matches(**base, session_key=f"s{i}") for i in range(2000))
    assert 0.25 < sampled / 2000 < 0.35
    assert RuleScope(ratio=0.0).matches(**base, session_key="s") is False
    assert RuleScope(ratio=1.0).matches(**base, session_key="s") is True


def test_disabled_rule_never_matches():
    rule = PolicyRule(name="off", action=RuleAction(ceiling="c1"), enabled=False)
    assert rule.matches(tenant_id="t", profile="p", session_key="s", now_ms=at(0)) is False


def test_select_rule_prefers_priority_then_list_order():
    broad = PolicyRule(name="broad", action=RuleAction(ceiling="c2"), priority=0)
    narrow = PolicyRule(name="narrow", action=RuleAction(floor="c3"), priority=10)
    first = PolicyRule(name="first", action=RuleAction(ceiling="c1"), priority=0)
    args = {"tenant_id": "t", "profile": "p", "session_key": "s", "now_ms": at(0)}
    # Priority wins regardless of position, so appending a rule to the file
    # does not require re-sorting it.
    assert select_rule([broad, narrow], **args).name == "narrow"
    assert select_rule([narrow, broad], **args).name == "narrow"
    # Equal priority falls back to list order.
    assert select_rule([broad, first], **args).name == "broad"
    assert select_rule([], **args) is None


# --- Actions ---------------------------------------------------------------


def test_clamp_narrows_the_candidate_set():
    assert clamp_candidates(TIERS, None, "c1") == (["c0", "c1"], True)
    assert clamp_candidates(TIERS, "c2", None) == (["c2", "c3"], True)
    assert clamp_candidates(TIERS, "c1", "c2") == (["c1", "c2"], True)
    # A pin leaves exactly one candidate, which is what makes it a hard write.
    assert clamp_candidates(TIERS, "c2", "c2") == (["c2"], True)


def test_clamp_reports_when_the_band_is_impossible_for_the_client():
    # Ceiling c0 but the profile only serves c2/c3: routing must still answer,
    # and the operator needs to see that their rule cannot hold here.
    candidates, satisfiable = clamp_candidates(["c2", "c3"], None, "c0")
    assert candidates == ["c2", "c3"]
    assert satisfiable is False


def test_weights_shift_relative_to_the_classifier_tier():
    probs = {"c0": 0.05, "c1": 0.1, "c2": 0.6, "c3": 0.25}
    # 3.0 * 0.25 > 0.6 flips the argmax c2 -> c3: a +1 shift, applied to
    # whatever the postprocess landed on rather than re-deriving from scratch.
    assert shift_by_weights(probs, "c1", {"c3": 3.0}) == "c2"
    assert shift_by_weights(probs, "c2", {"c3": 3.0}) == "c3"
    assert shift_by_weights(probs, "c2", {"c0": 100.0}) == "c0"
    assert shift_by_weights(probs, "c2", {"c3": 1.1}) == "c2"  # too small to flip
    assert shift_by_weights(probs, "c3", {"c3": 3.0}) == "c3"  # clamped at the top
    assert shift_by_weights(dict.fromkeys(TIERS, 0.0), "c1", {"c3": 9.0}) == "c1"
    assert shift_by_weights(probs, "c1", {}) == "c1"


# --- Source: loading, hot reload, fail-open --------------------------------


def write_policy(tmp_path, rules) -> str:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    return str(path)


def test_loads_from_file_and_reports_live_state(tmp_path):
    path = write_policy(tmp_path, [{"name": "cap", "ceiling": "c1", "note": "cost"}])
    source = PolicySource(path=path, log=lambda _msg: None)
    summary = source.snapshot.summary()
    assert summary["ruleCount"] == 1
    assert summary["errors"] == []
    assert summary["rules"][0]["action"] == "ceiling=c1"
    assert summary["rules"][0]["note"] == "cost"
    assert summary["source"] == path


def test_reload_picks_up_edits_and_changes_the_version(tmp_path):
    path = write_policy(tmp_path, [{"name": "cap", "ceiling": "c1"}])
    source = PolicySource(path=path, log=lambda _msg: None)
    before = source.snapshot.version

    write_policy(tmp_path, [{"name": "cap", "ceiling": "c3"}])
    source.reload()
    assert source.snapshot.rules[0].action.ceiling == "c3"
    assert source.snapshot.version != before


def test_unchanged_file_does_not_churn_the_snapshot(tmp_path):
    path = write_policy(tmp_path, [{"name": "cap", "ceiling": "c1"}])
    source = PolicySource(path=path, log=lambda _msg: None)
    first = source.snapshot
    source.reload()
    # Identity, not equality: an unchanged reload must not hand the hot path a
    # new object (and must not reset loadedAt).
    assert source.snapshot is first


def test_broken_reload_keeps_last_known_good(tmp_path, capsys):
    path = write_policy(tmp_path, [{"name": "cap", "ceiling": "c1"}])
    source = PolicySource(path=path)
    good = source.snapshot

    (tmp_path / "policy.json").write_text("{not json", encoding="utf-8")
    source.reload()
    # Losing the ceiling that caps spend is worse than running a stale one.
    assert source.snapshot is good
    assert "not valid JSON" in capsys.readouterr().out

    (tmp_path / "policy.json").unlink()
    source.reload()
    assert source.snapshot is good


def test_partially_broken_file_loads_the_good_rules_and_reports_the_rest(tmp_path):
    path = write_policy(
        tmp_path, [{"name": "ok", "ceiling": "c2"}, {"name": "bad", "floor": "c3", "ceiling": "c0"}]
    )
    source = PolicySource(path=path, log=lambda _msg: None)
    assert [r.name for r in source.snapshot.rules] == ["ok"]
    assert source.snapshot.errors and "bad" in source.snapshot.errors[0]


def test_inline_env_policy_is_the_bootstrap_path():
    source = PolicySource(inline=json.dumps([{"name": "pin", "pin": "c0"}]), log=lambda _m: None)
    assert source.snapshot.source == "env"
    assert source.snapshot.rules[0].action.pins is True
    # A broken inline value yields no rules rather than a crash at startup.
    assert PolicySource(inline="{oops", log=lambda _m: None).snapshot.rules == []


def test_file_wins_over_inline_and_no_path_means_no_watcher(tmp_path):
    path = write_policy(tmp_path, [{"name": "from-file", "ceiling": "c1"}])
    source = PolicySource(
        inline=json.dumps([{"name": "from-env", "ceiling": "c3"}]), path=path, log=lambda _m: None
    )
    assert [r.name for r in source.snapshot.rules] == ["from-file"]

    inline_only = PolicySource(inline="[]", log=lambda _m: None)
    inline_only.start()  # no-op without a path
    assert inline_only._thread is None
    inline_only.stop()


def test_build_policy_source_reads_the_documented_env_vars(tmp_path):
    path = write_policy(tmp_path, [{"name": "env-wired", "pin": "c2"}])
    source = build_policy_source(
        {"SQUILLA_POLICY_FILE": path, "SQUILLA_POLICY_RELOAD_SECONDS": "5"},
        log=lambda _m: None,
    )
    assert [r.name for r in source.snapshot.rules] == ["env-wired"]
    assert source._reload_seconds == 5.0

    empty = build_policy_source({}, log=lambda _m: None)
    assert empty.snapshot.rules == []
    assert empty._reload_seconds == DEFAULT_RELOAD_SECONDS
