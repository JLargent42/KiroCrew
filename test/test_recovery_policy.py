"""Recovery schedule, config snapshot and tracker regression coverage."""

import random
from types import SimpleNamespace

import pytest

from kiro_crew.recovery import policy as pol
from kiro_crew.recovery.ladder import L1_TOOL_CALL, L4_GATEWAYD, L5_GATEWAY, LADDER, LAYERS


def _layer(**overrides):
    return pol.LayerPolicy(**dict(layer="t", trigger="test", max_attempts=3) | overrides)


@pytest.mark.parametrize(
    "attempt,expected", [(0, 2), (-9, 2), (1, 2), (2, 4), (3, 8), (4, 16), (7, 120), (50, 120)]
)
def test_raw_backoff(attempt, expected):
    assert _layer().raw_backoff_secs(attempt) == expected


def test_jitter_bounds_spread_and_cap():
    lp = _layer()
    rng = random.Random(1234)
    for attempt in range(1, 8):
        raw = lp.raw_backoff_secs(attempt)
        draws = [lp.backoff_secs(attempt, rng=rng) for _ in range(200)]
        assert all(raw / 2 <= d <= raw for d in draws)
        assert len(set(draws)) > 10
    assert _layer(jitter=False).backoff_secs(3) == 8
    assert all(_layer(max_secs=5).backoff_secs(a, rng=rng) <= 5 for a in range(1, 20))


@pytest.mark.parametrize(
    "attempt,hint,expected", [(1, 30, 30), (6, 3, 64), (1, 10000, 120), (1, 0, 2), (1, -5, 2)]
)
def test_retry_hint_is_a_capped_floor(attempt, hint, expected):
    assert _layer(jitter=False).backoff_secs(attempt, retry_after_secs=hint) == expected


def test_escalation_and_zero_budget():
    assert [_layer().should_escalate(n) for n in (1, 2, 3, 4)] == [False, False, True, True]
    assert _layer(max_attempts=0).should_escalate(0)
    assert _layer(max_attempts=0).should_escalate(1)


def test_tracker_failure_run_cooldown_and_success():
    t = pol.RecoveryTracker(cooldown_secs=600)
    assert t.record_failure("a", now=0) == 1
    assert t.record_failure("a", now=10) == 2
    assert t.failing_since("a", now=11) == 0
    assert t.attempts("b", now=11) == 0
    assert t.attempts("a", now=609) == 2
    assert t.attempts("a", now=610) == 0
    assert t.record_failure("a", now=611) == 1
    t.record_success("a")
    assert t.attempts("a", now=612) == 0
    assert t.failing_since("a", now=612) is None
    t.record_failure("a", now=613)
    t.forget("a")
    assert len(t) == 0


def test_tracker_bounded_size():
    t = pol.RecoveryTracker()
    for i in range(pol.TRACKER_MAX_UNITS + 50):
        t.record_failure(f"u{i}", now=float(i))
    assert len(t) == pol.TRACKER_MAX_UNITS
    assert t.attempts("u0", now=0) == 0


def test_policy_table_and_bare_schedule():
    assert tuple(LADDER.layers) == LAYERS
    with pytest.raises(KeyError, match="unknown recovery layer"):
        LADDER.layer("L9")
    lp = LADDER.layer(L5_GATEWAY)
    assert lp.automatic is False and lp.max_attempts == 0 and lp.escalate_to is None
    assert (pol.DEFAULT_BACKOFF_BASE_SECS, pol.DEFAULT_BACKOFF_MAX_SECS) == (2, 120)
    assert (LADDER.layer(L1_TOOL_CALL).base_secs, LADDER.layer(L1_TOOL_CALL).max_secs) == (2, 120)
    p = pol.RecoveryPolicy(base_secs=1, max_secs=8, jitter=False)
    assert p.backoff_secs(1) == 1
    assert p.backoff_secs(10) == 8


def test_config_snapshot_and_missing_keys():
    cfg = SimpleNamespace(
        agent=SimpleNamespace(recovery_backoff_base_secs=4, recovery_backoff_max_secs=30)
    )
    p = pol.RecoveryPolicy.from_config(cfg)
    assert (p.layer(L1_TOOL_CALL).base_secs, p.layer(L1_TOOL_CALL).max_secs) == (4, 30)
    assert p.layer(L1_TOOL_CALL).raw_backoff_secs(10) == 30
    assert pol.RecoveryPolicy.from_config(None) is LADDER
    assert pol.RecoveryPolicy.from_config(SimpleNamespace(agent=SimpleNamespace())) is LADDER


@pytest.mark.parametrize("value", [True, "x", float("nan"), float("inf"), -float("inf")])
def test_config_rejects_invalid_numbers(value):
    cfg = SimpleNamespace(
        agent=SimpleNamespace(recovery_backoff_base_secs=value, recovery_backoff_max_secs=value)
    )
    assert pol.RecoveryPolicy.from_config(cfg) is LADDER


def test_config_clamp_and_pinned_rung():
    p = LADDER.with_schedule(base_secs=500, max_secs=0.001)
    assert p.base_secs == pol.BACKOFF_BASE_MAX_SECS
    assert p.max_secs == p.base_secs
    p = LADDER.with_schedule(base_secs=10, max_secs=600, jitter=False, cooldown_secs=900)
    assert p.layer(L1_TOOL_CALL).max_secs == 600
    assert p.layer(L1_TOOL_CALL).cooldown_secs == 900
    assert p.backoff_secs(1, layer=L1_TOOL_CALL) == 10
    l4 = p.layer(L4_GATEWAYD)
    assert l4 is LADDER.layer(L4_GATEWAYD)
    assert l4.pinned and l4.jitter
    assert (l4.base_secs, l4.max_secs, l4.cooldown_secs) == (1, 60, 600)


def test_policy_module_is_pure_at_import(tmp_path):
    import os
    import subprocess
    import sys

    code = """import sys
import kiro_crew.recovery.policy
assert not any(n.startswith(("kiro_crew.config", "kiro_crew.taskq", "kiro_crew.adaptive", "kiro_crew.subagent_manager")) for n in sys.modules)
print("pure import")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == "pure import"
