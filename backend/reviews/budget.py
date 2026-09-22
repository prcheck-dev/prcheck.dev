"""Run budgets enforced in code, not in the prompt.

Every model call ticks the budget; when a hard limit is crossed the run stops
rather than quietly spending more. Shard reviewers run on separate threads, so
the counters are guarded by a lock.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock


@dataclass(frozen=True)
class Budgets:
    # Deep mode makes one generate call per changed file plus a verify pass, so
    # the turn budget must comfortably exceed the per-PR file cap.
    max_model_turns: int = 300
    max_tokens: int = 4_000_000
    max_wall_clock_s: float = 1800.0


class BudgetExhausted(RuntimeError):
    def __init__(self, dimension: str, detail: str):
        super().__init__(f"budget exhausted: {dimension} ({detail})")
        self.dimension = dimension
        self.detail = detail


@dataclass
class Budget:
    limits: Budgets = field(default_factory=Budgets)
    started_at: float = field(default_factory=time.monotonic)
    model_turns: int = 0
    tokens: int = 0
    usd_est: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider: str = ""
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @property
    def wall_clock_s(self) -> float:
        return time.monotonic() - self.started_at

    def _check(self) -> None:
        limits = self.limits
        if self.model_turns > limits.max_model_turns:
            raise BudgetExhausted("model_turns", f"{self.model_turns}>{limits.max_model_turns}")
        if self.tokens > limits.max_tokens:
            raise BudgetExhausted("tokens", f"{self.tokens}>{limits.max_tokens}")
        if self.wall_clock_s > limits.max_wall_clock_s:
            raise BudgetExhausted("wall_clock", f"{self.wall_clock_s:.0f}s>{limits.max_wall_clock_s}s")

    def tick_turn(
        self,
        tokens: int = 0,
        usd: float = 0.0,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        provider: str = "",
    ) -> None:
        with self._lock:
            self.model_turns += 1
            self.tokens += tokens
            self.usd_est += usd
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens
            if provider:
                self.provider = provider
            self._check()

    def as_dict(self) -> dict:
        return {
            "tokens": self.tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "usd_est": round(self.usd_est, 6),
            "provider": self.provider,
            "wall_clock_s": round(self.wall_clock_s, 1),
            "model_turns": self.model_turns,
        }
