"""Bounded recovery decisions; callers own probes, cleanup and restart execution.

This library starts no services. L5 only notifies a human. Shared schedules are
snapshotted once at process startup; L4 retains its reconnect-budget-derived cap.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from kiro_crew.mcp_gateway.shutdown_budget import (
    POOL_SHUTDOWN_SECS,
    TOTAL_SHUTDOWN_BUDGET_SECS,
)
from kiro_crew.recovery.policy import (
    DEFAULT_COOLDOWN_SECS,
    LayerPolicy,
    RecoveryPolicy,
    RecoveryTracker,
)

logger = logging.getLogger(__name__)
L1_TOOL_CALL = "L1_tool_call"
L2_BACKEND = "L2_backend"
L3_ACP_RUNTIME = "L3_acp_runtime"
L4_GATEWAYD = "L4_gatewayd"
L5_GATEWAY = "L5_gateway"
LAYERS = (L1_TOOL_CALL, L2_BACKEND, L3_ACP_RUNTIME, L4_GATEWAYD, L5_GATEWAY)
ACTION_RETRY = "retry"
ACTION_ESCALATE = "escalate"
ACTION_NOTIFY = "notify"
ACTION_GIVE_UP = "give_up"
CLASS_CAPACITY = "capacity"
CLASS_RECOVERABLE_INFRA = "recoverable_infra"
JSONRPC_CAPACITY_CODE = -32001
GATEWAYD_RESPAWN_COOLDOWN_SECS = 600.0
# The stub's reconnect budget covers three kill/respawn cycles at this cap.
GATEWAYD_BACKOFF_BASE_SECS = 1.0
GATEWAYD_BACKOFF_MAX_SECS = 60.0
# In-place session continues, distinct from L3 runtime rebuild attempts.
SESSION_RECOVERY_MAX_ATTEMPTS = 3

LADDER = RecoveryPolicy(
    layers={
        L1_TOOL_CALL: LayerPolicy(
            layer=L1_TOOL_CALL,
            trigger="JSON-RPC recoverable_infra: -32001 capacity, backend gone, spawn-queue timeout",
            max_attempts=3,
            escalate_to=L2_BACKEND,
        ),
        L2_BACKEND: LayerPolicy(
            layer=L2_BACKEND,
            trigger="BackendGone, initialize timeout, breaker OPEN",
            max_attempts=2,
            cleanup_deadline_secs=POOL_SHUTDOWN_SECS,
            escalate_to=L3_ACP_RUNTIME,
        ),
        L3_ACP_RUNTIME: LayerPolicy(
            layer=L3_ACP_RUNTIME,
            trigger="AcpRuntimeDead, stall past idle window, session/new collector abandoned",
            max_attempts=2,
            cleanup_deadline_secs=TOTAL_SHUTDOWN_BUDGET_SECS,
            escalate_to=L4_GATEWAYD,
        ),
        L4_GATEWAYD: LayerPolicy(
            layer=L4_GATEWAYD,
            trigger="liveness ping failed 3x AND self-report absent/stale AND no backend progress",
            max_attempts=2,
            base_secs=GATEWAYD_BACKOFF_BASE_SECS,
            max_secs=GATEWAYD_BACKOFF_MAX_SECS,
            pinned=True,
            cooldown_secs=GATEWAYD_RESPAWN_COOLDOWN_SECS,
            cleanup_deadline_secs=TOTAL_SHUTDOWN_BUDGET_SECS,
            escalate_to=L5_GATEWAY,
        ),
        L5_GATEWAY: LayerPolicy(
            layer=L5_GATEWAY,
            trigger="none automatic: notify the user to restart",
            max_attempts=0,
            automatic=False,
        ),
    }
)


@dataclass(frozen=True)
class InfraError:
    """A classified infrastructure failure and the server's retry hint."""

    error_class: str
    retry_after_secs: float | None = None
    code: int | None = None
    detail: str = ""


_INFRA_TEXT_MAX_CHARS = 2000
_CAPACITY_CODE_RE = re.compile(r"(?<![\d.])-32001(?![\d.])")
_CAPACITY_CLASS_RE = re.compile(r"""["']?class["']?\s*[:=]\s*["']?capacity\b""", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"""retry_after_secs["']?\s*[:=]\s*["']?(\d+(?:\.\d+)?)""")
_RECOVERABLE_INFRA_MARKERS = (
    re.compile(r"\bBackendGone\b"),
    re.compile(r"\bbackend (?:is )?gone\b", re.IGNORECASE),
    re.compile(r"\bSpawnGateTimeout\b"),
    re.compile(r"\bspawn[- ]queue (?:wait )?timed? ?out\b", re.IGNORECASE),
    re.compile(r"\bqueued timeout\b", re.IGNORECASE),
    re.compile(r"\bgateway daemon (?:is )?unavailable\b", re.IGNORECASE),
    re.compile(r"\breconnect budget exhausted\b", re.IGNORECASE),
)


def _classify_error_object(err: Mapping[str, Any]) -> InfraError | None:
    code = err.get("code")
    data = err.get("data")
    klass = data.get("class") if isinstance(data, Mapping) else None
    retry_after = None
    if isinstance(data, Mapping):
        raw = data.get("retry_after_secs")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
            retry_after = float(raw)
    message = str(err.get("message") or "")
    if code == JSONRPC_CAPACITY_CODE or klass == CLASS_CAPACITY:
        return InfraError(
            CLASS_CAPACITY,
            retry_after,
            code if isinstance(code, int) else JSONRPC_CAPACITY_CODE,
            message[:200],
        )
    if message and any(p.search(message) for p in _RECOVERABLE_INFRA_MARKERS):
        return InfraError(
            CLASS_RECOVERABLE_INFRA,
            retry_after,
            code if isinstance(code, int) else None,
            message[:200],
        )
    return None


def classify_infra_error(payload: Any) -> InfraError | None:
    """Recognise only infrastructure error shapes, not ordinary tool failures.

    RPC results and bare MCP results own their success/failure disposition;
    only their failure fields may enter text/code heuristics. Mapping and JSON
    forms share that dispatch. Raw text and exceptions are a compatibility path:
    the caller MUST already know the operation failed, never pass arbitrary
    output or documentation. Classification is not permission to replay unknown
    side effects or provider requests.
    """
    if payload is None:
        return None
    if isinstance(payload, Mapping):
        return _classify_mapping(payload)
    text = str(payload).strip()
    if not text or len(text) > _INFRA_TEXT_MAX_CHARS:
        return None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, Mapping):
            return _classify_mapping(obj)
    return _classify_error_text(text)


def _classify_mapping(payload: Mapping[str, Any]) -> InfraError | None:
    """Select an outcome owner before inspecting any error text or codes.

    RPC result and MCP result fields own their outcome, including success.
    An error envelope authorizes only its error member, never sibling metadata.
    Bare negative-code or capacity-class RPC errors remain accepted; message-only
    mappings do not establish failure provenance. No recognized envelope falls
    through.
    """
    if "result" in payload:
        result = payload.get("result")
        if isinstance(result, Mapping) and result.get("isError") is True:
            return _classify_mcp_error_result(result)
        return None
    if "isError" in payload or "content" in payload:
        if payload.get("isError") is True:
            return _classify_mcp_error_result(payload)
        return None
    if "error" in payload:
        inner = payload["error"]
        return _classify_error_object(inner) if isinstance(inner, Mapping) else None
    if "jsonrpc" in payload:
        return None
    code = payload.get("code")
    data = payload.get("data")
    if (isinstance(code, int) and not isinstance(code, bool) and code < 0) or (
        isinstance(data, Mapping) and data.get("class") == CLASS_CAPACITY
    ):
        return _classify_error_object(payload)
    return None


def _classify_mcp_error_result(result: Mapping[str, Any]) -> InfraError | None:
    content = result.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    text = "\n".join(parts).strip()
    if not text or len(text) > _INFRA_TEXT_MAX_CHARS:
        return None
    return _classify_error_text(text)


def _classify_error_text(text: str) -> InfraError | None:
    retry_after = None
    match = _RETRY_AFTER_RE.search(text)
    if match:
        retry_after = float(match.group(1))
    if _CAPACITY_CODE_RE.search(text) or _CAPACITY_CLASS_RE.search(text):
        return InfraError(CLASS_CAPACITY, retry_after, JSONRPC_CAPACITY_CODE, text[:200])
    if any(p.search(text) for p in _RECOVERABLE_INFRA_MARKERS):
        return InfraError(CLASS_RECOVERABLE_INFRA, retry_after, detail=text[:200])
    return None


@dataclass(frozen=True)
class RecoveryDecision:
    layer: str
    unit: str
    action: str
    attempt: int
    delay_secs: float = 0.0
    next_layer: str | None = None
    reason: str = ""

    @property
    def retry(self) -> bool:
        return self.action == ACTION_RETRY


EventSink = Callable[[str, dict[str, Any]], None]
Notifier = Callable[[str, str], None]


def _log_notifier(layer: str, message: str) -> None:
    logger.error("recovery ladder escalation to %s: %s", layer, message)


class RecoveryLadder:
    """Count failures and emit decisions; never execute recovery actions itself."""

    def __init__(
        self,
        policy: RecoveryPolicy = LADDER,
        *,
        event_sink: EventSink | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._policy = policy
        self._event_sink = event_sink
        self._notifier = notifier or _log_notifier
        self._clock = clock
        self._rng = rng
        self._lock = threading.Lock()
        self._trackers = {
            name: RecoveryTracker(cooldown_secs=lp.cooldown_secs)
            for name, lp in policy.layers.items()
        }

    @property
    def policy(self) -> RecoveryPolicy:
        return self._policy

    def layer_policy(self, layer: str) -> LayerPolicy:
        return self._policy.layer(layer)

    def attempts(self, layer: str, unit: str, *, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        with self._lock:
            return self._trackers[layer].attempts(unit, now)

    def backoff_secs(
        self, layer: str, attempt: int, *, retry_after_secs: float | None = None
    ) -> float:
        return self._policy.layer(layer).backoff_secs(
            attempt, retry_after_secs=retry_after_secs, rng=self._rng
        )

    def observe_failure(
        self,
        layer: str,
        unit: str,
        *,
        now: float | None = None,
        retry_after_secs: float | None = None,
        reason: str = "",
        task_id: str | None = None,
    ) -> RecoveryDecision:
        from kiro_crew.metrics.events import (
            RECOVERY_ATTEMPTS,
            RECOVERY_ESCALATIONS,
            emit_counter,
        )

        lp = self._policy.layer(layer)
        now = self._clock() if now is None else now
        with self._lock:
            tracker = self._trackers[layer]
            attempt = tracker.record_failure(unit, now)
            needs_notice = not lp.automatic or (
                lp.should_escalate(attempt)
                and lp.escalate_to is not None
                and not self._policy.layer(lp.escalate_to).automatic
            )
            notify = needs_notice and tracker._claim_notification(unit)
        if not lp.automatic:
            decision = RecoveryDecision(
                layer, unit, ACTION_GIVE_UP, attempt, reason=reason or lp.trigger
            )
            if notify:
                self._notify(layer, reason or lp.trigger)
        elif not lp.should_escalate(attempt):
            delay = lp.backoff_secs(attempt, retry_after_secs=retry_after_secs, rng=self._rng)
            decision = RecoveryDecision(
                layer, unit, ACTION_RETRY, attempt, delay_secs=delay, reason=reason
            )
        else:
            nxt = lp.escalate_to
            if nxt is None:
                decision = RecoveryDecision(layer, unit, ACTION_GIVE_UP, attempt, reason=reason)
            elif not self._policy.layer(nxt).automatic:
                decision = RecoveryDecision(
                    layer, unit, ACTION_NOTIFY, attempt, next_layer=nxt, reason=reason
                )
                if notify:
                    self._notify(
                        nxt,
                        f"{layer} exhausted {attempt} attempt(s) on {unit}; "
                        f"automatic recovery stops here ({reason or lp.trigger})",
                    )
            else:
                decision = RecoveryDecision(
                    layer, unit, ACTION_ESCALATE, attempt, next_layer=nxt, reason=reason
                )
            if nxt is not None:
                emit_counter(RECOVERY_ESCALATIONS, {"from_layer": layer, "to_layer": nxt})
        emit_counter(RECOVERY_ATTEMPTS, {"layer": layer, "action": decision.action})
        self._sink(
            task_id,
            {
                "layer": layer,
                "attempt": attempt,
                "action": decision.action,
                "delay_secs": round(decision.delay_secs, 3),
                "next_layer": decision.next_layer,
                "reason": (reason or "")[:200],
            },
        )
        return decision

    def observe_success(
        self, layer: str, unit: str, *, now: float | None = None, task_id: str | None = None
    ) -> float | None:
        from kiro_crew.metrics.events import RECOVERY_DURATION_SECS, emit_histogram

        now = self._clock() if now is None else now
        with self._lock:
            tracker = self._trackers[layer]
            since = tracker.failing_since(unit, now)
            tracker.record_success(unit)
        if since is None:
            return None
        duration = max(0.0, now - since)
        emit_histogram(RECOVERY_DURATION_SECS, duration, {"layer": layer}, unit="s")
        self._sink(task_id, {"layer": layer, "action": "recovered", "duration_secs": duration})
        return duration

    def record_restart(self, layer: str) -> None:
        from kiro_crew.metrics.events import RESTARTS_TOTAL, emit_counter

        if layer not in self._policy.layers:
            raise KeyError(f"unknown recovery layer {layer!r}")
        emit_counter(RESTARTS_TOTAL, {"layer": layer})

    def forget(self, layer: str, unit: str) -> None:
        with self._lock:
            self._trackers[layer].forget(unit)

    def table(self) -> list[dict[str, Any]]:
        rows = []
        for name in LAYERS:
            lp = self._policy.layer(name)
            rows.append(
                {
                    "layer": name,
                    "trigger": lp.trigger,
                    "cleanup_deadline_secs": lp.cleanup_deadline_secs,
                    "backoff_base_secs": lp.base_secs,
                    "backoff_max_secs": lp.max_secs,
                    "jitter": lp.jitter,
                    "attempts_before_escalation": lp.max_attempts,
                    "cooldown_secs": lp.cooldown_secs,
                    "escalates_to": lp.escalate_to,
                    "automatic": lp.automatic,
                }
            )
        return rows

    def _notify(self, layer: str, message: str) -> None:
        """Deliver an already-claimed episode notification outside the lock."""
        try:
            self._notifier(layer, message)
        except Exception:
            logger.debug("recovery notifier failed", exc_info=True)

    def _sink(self, task_id: str | None, data: dict[str, Any]) -> None:
        if self._event_sink is None or not task_id:
            return
        try:
            self._event_sink(task_id, data)
        except Exception:
            logger.debug("recovery event sink failed for %s", task_id, exc_info=True)


_default: RecoveryLadder | None = None
_default_lock = threading.Lock()


def initialize_default_ladder(cfg: Any) -> RecoveryLadder:
    """Snapshot an already-loaded startup config once per process.

    Call before admitting consumers. Repeated startup calls return the same
    instance without rereading cfg, replacing policy or resetting attempts.
    Changed configuration requires process restart, not hot application.
    """
    global _default
    with _default_lock:
        if _default is None:
            _default = RecoveryLadder(RecoveryPolicy.from_config(cfg))
        return _default


def default_ladder() -> RecoveryLadder:
    """Return the initialized process ladder without config I/O.

    Early access is an ordering error, not permission to freeze default settings.
    Explicit standalone clients may instead construct RecoveryLadder directly.
    """
    with _default_lock:
        if _default is None:
            raise RuntimeError("initialize_default_ladder must run before recovery consumers")
        return _default


def _reset_default_ladder_for_tests() -> None:
    global _default
    with _default_lock:
        _default = None


def record_restart(layer: str) -> None:
    default_ladder().record_restart(layer)


__all__ = [
    "ACTION_ESCALATE",
    "ACTION_GIVE_UP",
    "ACTION_NOTIFY",
    "ACTION_RETRY",
    "CLASS_CAPACITY",
    "CLASS_RECOVERABLE_INFRA",
    "DEFAULT_COOLDOWN_SECS",
    "GATEWAYD_BACKOFF_BASE_SECS",
    "GATEWAYD_BACKOFF_MAX_SECS",
    "SESSION_RECOVERY_MAX_ATTEMPTS",
    "GATEWAYD_RESPAWN_COOLDOWN_SECS",
    "JSONRPC_CAPACITY_CODE",
    "L1_TOOL_CALL",
    "L2_BACKEND",
    "L3_ACP_RUNTIME",
    "L4_GATEWAYD",
    "L5_GATEWAY",
    "LADDER",
    "LAYERS",
    "InfraError",
    "RecoveryDecision",
    "RecoveryLadder",
    "classify_infra_error",
    "default_ladder",
    "initialize_default_ladder",
    "record_restart",
]
