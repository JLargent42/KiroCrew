"""Pure bounded recovery schedules; callers supply clocks and optional RNGs."""

from __future__ import annotations

import math
import random
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any

DEFAULT_BACKOFF_BASE_SECS = 2.0
DEFAULT_BACKOFF_MAX_SECS = 120.0
BACKOFF_BASE_MIN_SECS = 0.1
BACKOFF_BASE_MAX_SECS = 60.0
BACKOFF_MAX_MIN_SECS = 1.0
BACKOFF_MAX_MAX_SECS = 3600.0
DEFAULT_COOLDOWN_SECS = 600.0
# Larger exponents cannot change a capped delay.
_MAX_EXPONENT = 16
TRACKER_MAX_UNITS = 1024


@dataclass(frozen=True)
class LayerPolicy:
    """One rung: retry schedule, cleanup budget and escalation destination."""

    layer: str
    trigger: str
    max_attempts: int
    base_secs: float = DEFAULT_BACKOFF_BASE_SECS
    max_secs: float = DEFAULT_BACKOFF_MAX_SECS
    jitter: bool = True
    cooldown_secs: float = DEFAULT_COOLDOWN_SECS
    cleanup_deadline_secs: float | None = None
    escalate_to: str | None = None
    automatic: bool = True
    pinned: bool = False

    def raw_backoff_secs(self, attempt: int) -> float:
        exponent = max(0, min(int(attempt) - 1, _MAX_EXPONENT))
        return float(min(self.max_secs, self.base_secs * (2**exponent)))

    def backoff_secs(
        self,
        attempt: int,
        *,
        retry_after_secs: float | None = None,
        rng: random.Random | None = None,
    ) -> float:
        """Equal jitter, then server-hint floor, then the hard cap."""
        raw = self.raw_backoff_secs(attempt)
        delay = raw
        if self.jitter and raw > 0:
            delay = raw / 2.0 + (rng or random).random() * (raw / 2.0)
        if retry_after_secs is not None and retry_after_secs > 0:
            delay = max(delay, float(retry_after_secs))
        return float(min(self.max_secs, delay))

    def should_escalate(self, attempts: int) -> bool:
        return self.max_attempts <= 0 or int(attempts) >= self.max_attempts


@dataclass(frozen=True)
class RecoveryPolicy:
    """Shared schedule with per-layer specialisations; L4 stays pinned."""

    base_secs: float = DEFAULT_BACKOFF_BASE_SECS
    max_secs: float = DEFAULT_BACKOFF_MAX_SECS
    jitter: bool = True
    cooldown_secs: float = DEFAULT_COOLDOWN_SECS
    layers: dict[str, LayerPolicy] = field(default_factory=dict)

    def layer(self, name: str) -> LayerPolicy:
        try:
            return self.layers[name]
        except KeyError:
            raise KeyError(f"unknown recovery layer {name!r}") from None

    def backoff_secs(
        self,
        attempt: int,
        *,
        layer: str | None = None,
        retry_after_secs: float | None = None,
        rng: random.Random | None = None,
    ) -> float:
        lp = (
            self.layer(layer)
            if layer is not None
            else LayerPolicy(
                layer="",
                trigger="",
                max_attempts=1,
                base_secs=self.base_secs,
                max_secs=self.max_secs,
                jitter=self.jitter,
                cooldown_secs=self.cooldown_secs,
            )
        )
        return lp.backoff_secs(attempt, retry_after_secs=retry_after_secs, rng=rng)

    def with_schedule(
        self,
        *,
        base_secs: float | None = None,
        max_secs: float | None = None,
        jitter: bool | None = None,
        cooldown_secs: float | None = None,
    ) -> RecoveryPolicy:
        """Copy the schedule without changing pinned rungs."""
        base = self.base_secs if base_secs is None else float(base_secs)
        cap = self.max_secs if max_secs is None else float(max_secs)
        base = min(max(base, BACKOFF_BASE_MIN_SECS), BACKOFF_BASE_MAX_SECS)
        cap = max(base, min(max(cap, BACKOFF_MAX_MIN_SECS), BACKOFF_MAX_MAX_SECS))
        jitter = self.jitter if jitter is None else bool(jitter)
        cool = self.cooldown_secs if cooldown_secs is None else float(cooldown_secs)
        layers = {
            name: (
                lp
                if lp.pinned
                else replace(
                    lp,
                    base_secs=base,
                    max_secs=cap,
                    jitter=jitter,
                    cooldown_secs=cool,
                )
            )
            for name, lp in self.layers.items()
        }
        return RecoveryPolicy(base, cap, jitter, cool, layers)

    @classmethod
    def from_config(cls, cfg: Any, *, base: RecoveryPolicy | None = None) -> RecoveryPolicy:
        """Read an already-loaded config snapshot; never perform configuration I/O."""
        from kiro_crew.recovery.ladder import LADDER

        policy = base if base is not None else LADDER
        agent = getattr(cfg, "agent", None)
        base_secs = getattr(agent, "recovery_backoff_base_secs", None)
        max_secs = getattr(agent, "recovery_backoff_max_secs", None)
        if (
            not isinstance(base_secs, (int, float))
            or isinstance(base_secs, bool)
            or not math.isfinite(base_secs)
        ):
            base_secs = None
        if (
            not isinstance(max_secs, (int, float))
            or isinstance(max_secs, bool)
            or not math.isfinite(max_secs)
        ):
            max_secs = None
        if base_secs is None and max_secs is None:
            return policy
        return policy.with_schedule(base_secs=base_secs, max_secs=max_secs)


@dataclass
class _UnitState:
    attempts: int = 0
    last_failure_at: float = 0.0
    first_failure_at: float = 0.0
    notification_claimed: bool = False


class RecoveryTracker:
    """Per-unit consecutive failures, cooldown decay and bounded LRU storage."""

    def __init__(self, *, cooldown_secs: float = DEFAULT_COOLDOWN_SECS) -> None:
        self._cooldown = float(cooldown_secs)
        self._units: OrderedDict[str, _UnitState] = OrderedDict()

    @property
    def cooldown_secs(self) -> float:
        return self._cooldown

    def _fresh(self, unit: str, now: float) -> _UnitState:
        state = self._units.get(unit)
        if state is None:
            return _UnitState()
        if state.attempts and now - state.last_failure_at >= self._cooldown:
            del self._units[unit]
            return _UnitState()
        return state

    def attempts(self, unit: str, now: float) -> int:
        return self._fresh(unit, now).attempts

    def failing_since(self, unit: str, now: float) -> float | None:
        state = self._fresh(unit, now)
        return state.first_failure_at if state.attempts else None

    def record_failure(self, unit: str, now: float) -> int:
        state = self._fresh(unit, now)
        if state.attempts == 0:
            state.first_failure_at = now
        state.attempts += 1
        state.last_failure_at = now
        self._units[unit] = state
        self._units.move_to_end(unit)
        while len(self._units) > TRACKER_MAX_UNITS:
            self._units.popitem(last=False)
        return state.attempts

    def record_success(self, unit: str) -> None:
        self._units.pop(unit, None)

    def forget(self, unit: str) -> None:
        self._units.pop(unit, None)

    def __len__(self) -> int:
        return len(self._units)

    def _claim_notification(self, unit: str) -> bool:
        """Claim the recorded episode; caller holds its failure-update lock."""
        state = self._units[unit]
        if state.notification_claimed:
            return False
        state.notification_claimed = True
        return True


__all__ = [
    "BACKOFF_BASE_MAX_SECS",
    "BACKOFF_BASE_MIN_SECS",
    "BACKOFF_MAX_MAX_SECS",
    "BACKOFF_MAX_MIN_SECS",
    "DEFAULT_BACKOFF_BASE_SECS",
    "DEFAULT_BACKOFF_MAX_SECS",
    "DEFAULT_COOLDOWN_SECS",
    "TRACKER_MAX_UNITS",
    "LayerPolicy",
    "RecoveryPolicy",
    "RecoveryTracker",
]
