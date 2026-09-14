"""Deterministic clock and RNG for the recovery primitives; no admission imports."""

import random


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> float:
        self.t += secs
        return self.t


class FixedRng(random.Random):
    """Every draw returns the top of its range for exact retry instants."""

    def random(self) -> float:
        return 1.0

    def uniform(self, a: float, b: float) -> float:  # type: ignore[override]
        return b
