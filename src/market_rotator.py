"""Safe single-market score-based LP rotation state machine (#21)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Iterable, Optional


@dataclass(frozen=True)
class MarketScoreCandidate:
    market_id: str
    score: float
    spread_bps: float
    liquidity_usd: float


class SafeSingleMarketRotator:
    """
    Ensures at most ONE market runs at any given time and only rotates to a higher-scoring
    market when the score delta exceeds `min_switch_delta` and the current market has cleanly exited.
    """

    def __init__(self, min_switch_delta: float = 5.0, min_score: float = 10.0) -> None:
        self.min_switch_delta = float(min_switch_delta)
        self.min_score = float(min_score)
        self.active_market_id: Optional[str] = None
        self.active_score: float = 0.0

    def select_and_rotate(
        self,
        candidates: Iterable[MarketScoreCandidate],
        close_market_fn: Callable[[str], bool],
        open_market_fn: Callable[[str], bool],
    ) -> Optional[str]:
        valid = [c for c in candidates if c.score >= self.min_score]
        if not valid:
            if self.active_market_id is not None and close_market_fn(self.active_market_id):
                self.active_market_id = None
                self.active_score = 0.0
            return None

        valid.sort(key=lambda x: (-x.score, -x.liquidity_usd))
        best = valid[0]

        if self.active_market_id is None:
            if open_market_fn(best.market_id):
                self.active_market_id = best.market_id
                self.active_score = best.score
            return self.active_market_id

        if best.market_id == self.active_market_id:
            self.active_score = best.score
            return self.active_market_id

        if (best.score - self.active_score) >= self.min_switch_delta:
            # Strict mutex: close current market completely before opening next market
            if close_market_fn(self.active_market_id):
                self.active_market_id = None
                self.active_score = 0.0
                if open_market_fn(best.market_id):
                    self.active_market_id = best.market_id
                    self.active_score = best.score

        return self.active_market_id
