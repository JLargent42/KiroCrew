"""Recovery decisions and once-per-process startup contract."""

import json
import random
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from overload_fakes import Clock

from kiro_crew.metrics import events as ev
from kiro_crew.recovery import ladder as lad
from kiro_crew.recovery import policy as pol


@pytest.fixture(autouse=True)
def singleton():
    lad._reset_default_ladder_for_tests()
    yield
    lad._reset_default_ladder_for_tests()


@pytest.fixture
def rec(monkeypatch):
    recorder = Mock()
    monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: recorder)
    return recorder


@pytest.fixture
def clock():
    return Clock()


def _ladder(clock, **kwargs):
    return lad.RecoveryLadder(clock=clock, rng=random.Random(42), **kwargs)


@pytest.mark.parametrize(
    "payload,hint",
    [
        (
            {
                "code": -32001,
                "message": "capacity",
                "data": {"class": "capacity", "retry_after_secs": 7},
            },
            7,
        ),
        ({"jsonrpc": "2.0", "id": 4, "error": {"code": -32001}}, None),
        ('{"code": -32001, "data": {"class": "capacity", "retry_after_secs": 12}}', 12),
        ("MCP error -32001: gateway at capacity (class=capacity, retry_after_secs=5)", 5),
    ],
)
def test_capacity_shapes(payload, hint):
    got = lad.classify_infra_error(payload)
    assert got is not None
    assert (got.error_class, got.code, got.retry_after_secs) == (lad.CLASS_CAPACITY, -32001, hint)


@pytest.mark.parametrize(
    "text",
    [
        "BackendGone: backend exited",
        "spawn queue timed out after 600s",
        "reconnect budget exhausted; gateway daemon unavailable",
        RuntimeError("SpawnGateTimeout after 600s"),
    ],
)
def test_infrastructure_markers(text):
    got = lad.classify_infra_error(text)
    assert got is not None and got.error_class == lad.CLASS_RECOVERABLE_INFRA


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "Permission denied",
        "invalid arguments",
        "I cannot help with that request.",
        "Traceback: ValueError",
        "log line about BackendGone\n" * 200,
        "balance: -320011.5 units",
        "id 132001 processed",
        {"message": "ordinary failure"},
    ],
)
def test_non_infrastructure_results(payload):
    assert lad.classify_infra_error(payload) is None


def test_l1_delays_escalation_and_closed_metrics(clock, rec):
    ladder = _ladder(clock)
    hint = ladder.observe_failure(lad.L1_TOOL_CALL, "hint", retry_after_secs=30)
    assert hint.retry and hint.attempt == 1 and hint.delay_secs == 30
    first = ladder.observe_failure(lad.L1_TOOL_CALL, "a")
    second = ladder.observe_failure(lad.L1_TOOL_CALL, "a")
    assert first.retry and 1 <= first.delay_secs <= 2
    assert second.attempt == 2 and 2 <= second.delay_secs <= 4
    d = ladder.observe_failure(lad.L1_TOOL_CALL, "a")
    assert not d.retry and (d.action, d.next_layer) == (lad.ACTION_ESCALATE, lad.L2_BACKEND)
    rec.counter.assert_any_call(
        ev.RECOVERY_ESCALATIONS, attrs={"from_layer": lad.L1_TOOL_CALL, "to_layer": lad.L2_BACKEND}
    )
    rec.counter.assert_any_call(
        ev.RECOVERY_ATTEMPTS, attrs={"layer": lad.L1_TOOL_CALL, "action": lad.ACTION_RETRY}
    )
    assert ladder.observe_failure(lad.L1_TOOL_CALL, "b").attempt == 1


def test_success_and_cooldown(clock, rec):
    ladder = _ladder(clock)
    assert ladder.observe_success(lad.L1_TOOL_CALL, "new") is None
    rec.histogram.assert_not_called()
    ladder.observe_failure(lad.L1_TOOL_CALL, "a")
    clock.advance(45)
    assert ladder.observe_success(lad.L1_TOOL_CALL, "a") == 45
    assert ladder.attempts(lad.L1_TOOL_CALL, "a") == 0
    rec.histogram.assert_called_once_with(
        ev.RECOVERY_DURATION_SECS, 45, unit="s", attrs={"layer": lad.L1_TOOL_CALL}
    )
    ladder.observe_failure(lad.L1_TOOL_CALL, "a")
    ladder.observe_failure(lad.L1_TOOL_CALL, "a")
    clock.advance(601)
    assert ladder.observe_failure(lad.L1_TOOL_CALL, "a").attempt == 1


@pytest.mark.parametrize(
    "layer,next_layer",
    [(lad.L2_BACKEND, lad.L3_ACP_RUNTIME), (lad.L3_ACP_RUNTIME, lad.L4_GATEWAYD)],
)
def test_escalation_chain(layer, next_layer, clock, rec):
    ladder = _ladder(clock)
    assert ladder.observe_failure(layer, "u").retry
    d = ladder.observe_failure(layer, "u")
    assert (d.action, d.next_layer) == (lad.ACTION_ESCALATE, next_layer)


def test_l4_notify_once_success_rearms_and_cooldown(clock, rec):
    notify = Mock()
    ladder = _ladder(clock, notifier=notify)
    assert ladder.observe_failure(lad.L4_GATEWAYD, "daemon").retry
    clock.advance(60)
    d = ladder.observe_failure(lad.L4_GATEWAYD, "daemon")
    assert (d.action, d.next_layer) == (lad.ACTION_NOTIFY, lad.L5_GATEWAY)
    ladder.observe_failure(lad.L4_GATEWAYD, "daemon")
    assert notify.call_count == 1
    assert notify.call_args.args[0] == lad.L5_GATEWAY
    ladder.observe_success(lad.L4_GATEWAYD, "daemon")
    assert ladder.observe_failure(lad.L4_GATEWAYD, "daemon").retry
    ladder.observe_failure(lad.L4_GATEWAYD, "daemon")
    assert notify.call_count == 2
    clock.advance(lad.GATEWAYD_RESPAWN_COOLDOWN_SECS + 1)
    assert ladder.observe_failure(lad.L4_GATEWAYD, "daemon").retry


def test_l5_never_automatic_and_notifier_failure_is_contained(clock, rec):
    notify = Mock(side_effect=RuntimeError("channel unavailable"))
    ladder = _ladder(clock, notifier=notify)
    d = ladder.observe_failure(lad.L5_GATEWAY, "gateway")
    assert d.action == lad.ACTION_GIVE_UP and not d.retry and d.delay_secs == 0
    assert ladder.layer_policy(lad.L5_GATEWAY).automatic is False
    assert notify.call_args.args[0] == lad.L5_GATEWAY


def test_event_sink_and_no_task_id(clock, rec):
    rows = []
    ladder = _ladder(clock, event_sink=lambda task, data: rows.append((task, data)))
    d = ladder.observe_failure(lad.L1_TOOL_CALL, "u", task_id="t", reason="capacity")
    assert rows == [
        (
            "t",
            {
                "layer": lad.L1_TOOL_CALL,
                "attempt": 1,
                "action": lad.ACTION_RETRY,
                "delay_secs": round(d.delay_secs, 3),
                "next_layer": None,
                "reason": "capacity",
            },
        )
    ]
    ladder.observe_failure(lad.L1_TOOL_CALL, "no-task")
    assert len(rows) == 1


def test_restarts_and_table(clock, rec):
    ladder = lad.initialize_default_ladder(None)
    lad.record_restart(lad.L4_GATEWAYD)
    rec.counter.assert_called_with(ev.RESTARTS_TOTAL, attrs={"layer": lad.L4_GATEWAYD})
    with pytest.raises(KeyError):
        ladder.record_restart("L9")
    rows = _ladder(clock).table()
    assert [row["layer"] for row in rows] == list(lad.LAYERS)
    for row in rows:
        assert set(row) == {
            "layer",
            "trigger",
            "cleanup_deadline_secs",
            "backoff_base_secs",
            "backoff_max_secs",
            "jitter",
            "attempts_before_escalation",
            "cooldown_secs",
            "escalates_to",
            "automatic",
        }
    assert rows[0]["cleanup_deadline_secs"] is None
    assert rows[1]["cleanup_deadline_secs"] == lad.POOL_SHUTDOWN_SECS
    assert rows[3]["cleanup_deadline_secs"] == lad.TOTAL_SHUTDOWN_BUDGET_SECS
    assert rows[4]["automatic"] is False


def test_startup_required_before_default_access():
    with pytest.raises(RuntimeError, match="initialize_default_ladder"):
        lad.default_ladder()


def test_startup_snapshot_is_once_and_preserves_attempts(rec):
    cfg = SimpleNamespace(
        agent=SimpleNamespace(recovery_backoff_base_secs=8, recovery_backoff_max_secs=40)
    )
    ladder = lad.initialize_default_ladder(cfg)
    assert lad.default_ladder() is ladder
    for layer in (lad.L1_TOOL_CALL, lad.L2_BACKEND, lad.L3_ACP_RUNTIME):
        assert (ladder.layer_policy(layer).base_secs, ladder.layer_policy(layer).max_secs) == (
            8,
            40,
        )
        assert 4 <= ladder.backoff_secs(layer, 1) <= 8
        assert 20 <= ladder.backoff_secs(layer, 100) <= 40
    assert (
        ladder.layer_policy(lad.L4_GATEWAYD).base_secs,
        ladder.layer_policy(lad.L4_GATEWAYD).max_secs,
    ) == (1, 60)
    assert ladder.observe_failure(lad.L1_TOOL_CALL, "u").attempt == 1
    cfg.agent.recovery_backoff_base_secs = 60
    assert lad.initialize_default_ladder(cfg) is ladder
    assert ladder.layer_policy(lad.L1_TOOL_CALL).base_secs == 8
    assert ladder.observe_failure(lad.L1_TOOL_CALL, "u").attempt == 2
    assert ladder.observe_failure(lad.L1_TOOL_CALL, "u").action == lad.ACTION_ESCALATE


def test_concurrent_initializers_read_snapshot_once(monkeypatch, rec):
    snapshot = Mock(return_value=lad.LADDER.with_schedule(base_secs=6, max_secs=20))
    monkeypatch.setattr(lad.RecoveryPolicy, "from_config", snapshot)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ladders = list(pool.map(lad.initialize_default_ladder, [None] * 20))
    assert all(ladder is ladders[0] for ladder in ladders)
    snapshot.assert_called_once_with(None)
    snapshot.side_effect = AssertionError("must not read config during failure handling")
    for _ in range(3):
        lad.default_ladder().observe_failure(lad.L1_TOOL_CALL, "u")
        assert lad.initialize_default_ladder(object()) is ladders[0]
    assert lad.default_ladder().attempts(lad.L1_TOOL_CALL, "u") == 3
    assert snapshot.call_count == 1


def test_sink_failure_does_not_break_recovery(clock, rec):
    ladder = _ladder(clock, event_sink=Mock(side_effect=RuntimeError("store unavailable")))
    assert ladder.observe_failure(lad.L1_TOOL_CALL, "u", task_id="t").retry
    clock.advance(1)
    assert ladder.observe_success(lad.L1_TOOL_CALL, "u", task_id="t") == 1


@pytest.mark.parametrize(
    "text", ["BackendGone is documented here", "successful result mentions -32001"]
)
def test_serialized_successful_result_is_never_an_infra_error(text):
    mapping = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }
    assert lad.classify_infra_error(mapping) is None
    assert lad.classify_infra_error(json.dumps(mapping)) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonrpc": "2.0", "id": 2, "result": {"content": [], "isError": False}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"isError": False, "content": [{"type": "text", "text": "ok"}]},
        },
    ],
)
def test_mapping_and_serialized_agree_on_bare_success(payload):
    assert lad.classify_infra_error(payload) is None
    assert lad.classify_infra_error(json.dumps(payload)) is None


@pytest.mark.parametrize(
    "text,expected_class",
    [
        ("BackendGone: pooled backend exited before the reply", lad.CLASS_RECOVERABLE_INFRA),
        (
            "MCP error -32001: gateway at capacity (class=capacity, retry_after_secs=5)",
            lad.CLASS_CAPACITY,
        ),
    ],
)
def test_mcp_is_error_true_result_is_scanned_from_content_text(text, expected_class):
    mapping = {
        "jsonrpc": "2.0",
        "id": 9,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }
    got_mapping = lad.classify_infra_error(mapping)
    got_serialized = lad.classify_infra_error(json.dumps(mapping))
    assert got_mapping is not None and got_mapping.error_class == expected_class
    assert got_serialized is not None and got_serialized.error_class == expected_class


@pytest.mark.parametrize(
    "text", ["permission denied: /etc/shadow", "invalid arguments: expected string"]
)
def test_mcp_is_error_true_ordinary_failure_is_not_infra(text):
    mapping = {
        "jsonrpc": "2.0",
        "id": 10,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }
    assert lad.classify_infra_error(mapping) is None
    assert lad.classify_infra_error(json.dumps(mapping)) is None


def test_top_level_jsonrpc_error_member_still_classifies_mapping_and_serialized():
    envelope = {
        "jsonrpc": "2.0",
        "id": 11,
        "error": {"code": -32001, "data": {"class": "capacity", "retry_after_secs": 4}},
    }
    for candidate in (envelope, json.dumps(envelope)):
        got = lad.classify_infra_error(candidate)
        assert got is not None and (got.error_class, got.code, got.retry_after_secs) == (
            lad.CLASS_CAPACITY,
            -32001,
            4,
        )


def test_ordinary_top_level_error_member_is_not_infra():
    envelope = {"jsonrpc": "2.0", "id": 12, "error": {"code": -32602, "message": "invalid params"}}
    assert lad.classify_infra_error(envelope) is None
    assert lad.classify_infra_error(json.dumps(envelope)) is None


def test_notification_dedup_is_bounded_by_the_tracker_lifetime(clock, rec):
    notify = Mock()
    ladder = _ladder(clock, notifier=notify)
    total = pol.TRACKER_MAX_UNITS + 20
    for i in range(total):
        unit = f"u{i}"
        ladder.observe_failure(lad.L4_GATEWAYD, unit)
        clock.advance(60)
        ladder.observe_failure(lad.L4_GATEWAYD, unit)
        clock.advance(1)
    assert len(ladder._trackers[lad.L4_GATEWAYD]) == pol.TRACKER_MAX_UNITS
    assert notify.call_count == total
    last = f"u{total - 1}"
    ladder.forget(lad.L4_GATEWAYD, last)
    assert len(ladder._trackers[lad.L4_GATEWAYD]) == pol.TRACKER_MAX_UNITS - 1
    for _ in range(2):
        ladder.observe_failure(lad.L4_GATEWAYD, last)
    assert notify.call_count == total + 1


@pytest.mark.parametrize("serialized", [False, True], ids=["mapping", "json"])
@pytest.mark.parametrize(
    "payload,expected",
    [
        pytest.param(
            {
                "isError": False,
                "content": [{"type": "text", "text": "ok"}],
                "message": "BackendGone is documented",
            },
            None,
            id="bare-mcp-success",
        ),
        pytest.param(
            {"isError": False, "code": -32001, "data": {"class": "capacity"}},
            None,
            id="explicit-success-code-metadata",
        ),
        pytest.param(
            {
                "content": [{"type": "text", "text": "BackendGone"}],
                "message": "BackendGone",
                "code": -32001,
            },
            None,
            id="implicit-mcp-success",
        ),
        pytest.param(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"message": "BackendGone"},
                "error": {"code": -32001},
            },
            None,
            id="result-authority",
        ),
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "message": "BackendGone", "code": -32001},
            None,
            id="rpc-no-outcome",
        ),
        pytest.param(
            {"error": None, "message": "BackendGone", "code": -32001}, None, id="no-error-metadata"
        ),
        pytest.param({"message": "BackendGone is documented"}, None, id="message-only"),
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001}},
            lad.CLASS_CAPACITY,
            id="rpc-capacity",
        ),
        pytest.param(
            {"error": {"message": "BackendGone"}},
            lad.CLASS_RECOVERABLE_INFRA,
            id="explicit-infra-error",
        ),
        pytest.param({"code": -32001}, lad.CLASS_CAPACITY, id="bare-rpc-error"),
        pytest.param(
            {"isError": True, "content": [{"type": "text", "text": "BackendGone"}]},
            lad.CLASS_RECOVERABLE_INFRA,
            id="bare-mcp-infra",
        ),
        pytest.param(
            {
                "isError": True,
                "content": [{"type": "text", "text": "permission denied"}],
                "message": "BackendGone",
                "code": -32001,
            },
            None,
            id="ordinary-mcp-failure",
        ),
        pytest.param(
            {
                "error": {"code": -32602, "message": "invalid params"},
                "message": "BackendGone",
                "code": -32001,
            },
            None,
            id="ordinary-rpc-failure",
        ),
    ],
)
def test_structured_outcome_authority(payload, expected, serialized):
    got = lad.classify_infra_error(json.dumps(payload) if serialized else payload)
    assert (got.error_class if got else None) == expected


def test_notification_rearms_after_lru_eviction(clock, rec):
    notify = Mock()
    ladder = _ladder(clock, notifier=notify)
    for _ in range(2):
        ladder.observe_failure(lad.L4_GATEWAYD, "reused")
    assert notify.call_count == 1
    for i in range(pol.TRACKER_MAX_UNITS):
        ladder.observe_failure(lad.L4_GATEWAYD, f"other-{i}")
    assert ladder.attempts(lad.L4_GATEWAYD, "reused") == 0
    for _ in range(2):
        ladder.observe_failure(lad.L4_GATEWAYD, "reused")
    assert notify.call_count == 2


@pytest.mark.parametrize("origin", [lad.L4_GATEWAYD, lad.L5_GATEWAY])
@pytest.mark.parametrize("end", ["success", "forget", "cooldown-read", "cooldown-write"])
def test_notification_episode_end_rearms(origin, end, clock, rec):
    notify = Mock()
    ladder = _ladder(clock, notifier=notify)
    failures = max(1, ladder.layer_policy(origin).max_attempts)
    for _ in range(failures + 2):
        ladder.observe_failure(origin, "unit")
    assert notify.call_count == 1
    if end == "success":
        ladder.observe_success(origin, "unit")
    elif end == "forget":
        ladder.forget(origin, "unit")
    else:
        clock.advance(ladder.layer_policy(origin).cooldown_secs)
        if end == "cooldown-read":
            assert ladder.attempts(origin, "unit") == 0
    for attempt in range(1, failures + 1):
        assert ladder.observe_failure(origin, "unit").attempt == attempt
    assert notify.call_count == 2


@pytest.mark.parametrize("end", ["success", "forget"])
def test_notification_identity_is_origin_not_destination(end, clock, rec):
    notify = Mock()
    ladder = _ladder(clock, notifier=notify)
    for _ in range(2):
        ladder.observe_failure(lad.L4_GATEWAYD, "same")
    ladder.observe_failure(lad.L5_GATEWAY, "same")
    assert notify.call_count == 2
    if end == "success":
        ladder.observe_success(lad.L4_GATEWAYD, "same")
    else:
        ladder.forget(lad.L4_GATEWAYD, "same")
    ladder.observe_failure(lad.L5_GATEWAY, "same")
    assert notify.call_count == 2
    for _ in range(2):
        ladder.observe_failure(lad.L4_GATEWAYD, "same")
    assert notify.call_count == 3


def test_notification_claim_precedes_unlocked_callback(clock, rec):
    lock_observations = []
    notifications = []

    def notify(layer, message):
        acquired = ladder._lock.acquire(blocking=False)
        lock_observations.append(acquired)
        if not acquired:
            return
        ladder._lock.release()
        notifications.append((layer, message))
        ladder.observe_failure(lad.L4_GATEWAYD, "unit")

    ladder = _ladder(clock, notifier=notify)
    ladder.observe_failure(lad.L4_GATEWAYD, "unit")
    ladder.observe_failure(lad.L4_GATEWAYD, "unit")
    assert lock_observations == [True]
    assert len(notifications) == 1
    assert ladder.attempts(lad.L4_GATEWAYD, "unit") == 3


@pytest.mark.parametrize("origin", [lad.L4_GATEWAYD, lad.L5_GATEWAY])
def test_continuous_failures_keep_one_notification_episode(origin, clock, rec):
    notify = Mock()
    ladder = _ladder(clock, notifier=notify)
    gap = ladder.layer_policy(origin).cooldown_secs - 1
    for attempt in range(1, 7):
        assert ladder.observe_failure(origin, "unit").attempt == attempt
        clock.advance(gap)
    assert notify.call_count == 1


def test_concurrent_failures_claim_one_notification(clock, rec):
    notify = Mock()
    ladder = _ladder(clock, notifier=notify)
    with ThreadPoolExecutor(max_workers=4) as pool:
        decisions = list(
            pool.map(lambda _: ladder.observe_failure(lad.L4_GATEWAYD, "u"), range(20))
        )
    assert sorted(d.attempt for d in decisions) == list(range(1, 21))
    assert notify.call_count == 1


def test_ending_other_layer_does_not_rearm_live_episode(clock, rec):
    notify = Mock()
    ladder = _ladder(clock, notifier=notify)
    for _ in range(2):
        ladder.observe_failure(lad.L4_GATEWAYD, "same")
    ladder.forget(lad.L5_GATEWAY, "same")
    ladder.observe_success(lad.L3_ACP_RUNTIME, "same")
    ladder.observe_failure(lad.L4_GATEWAYD, "same")
    assert notify.call_count == 1


@pytest.mark.parametrize("serialized", [False, True], ids=["mapping", "json"])
def test_bare_capacity_class_preserves_hint_without_code(serialized):
    payload = {"data": {"class": "capacity", "retry_after_secs": 9}}
    candidate = json.dumps(payload) if serialized else payload
    got = lad.classify_infra_error(candidate)
    assert got is not None
    assert (got.error_class, got.code, got.retry_after_secs) == (lad.CLASS_CAPACITY, -32001, 9)
    payload["error"] = None
    assert lad.classify_infra_error(json.dumps(payload) if serialized else payload) is None
