"""Immutable ranking and rotation policy over observed facts, without side effects."""
from dataclasses import dataclass
from decimal import Decimal

from lp_v2.capital import can_open_market
from lp_v2.models import MarketPlan
from lp_v2.quoting import quote_pair
from lp_v2.scoring import SCORE_VERSION, eligible_market, score_market


@dataclass(frozen=True, slots=True)
class Candidate:
    condition_id: str
    score: float
    plan: MarketPlan
    reason: str | None


@dataclass(frozen=True, slots=True)
class Ranking:
    candidates: tuple[Candidate, ...]
    score_version: str = SCORE_VERSION

    @property
    def leader(self):
        return next((row for row in self.candidates if row.reason is None), None)


def rank_candidates(plans, *, startup_cap, exclusions=None):
    candidates = []
    for plan in plans:
        reason = (exclusions or {}).get(plan.market.condition_id)
        if reason is None and not eligible_market(plan.market):
            reason = 'ineligible_market'
        if reason is None:
            pair = quote_pair(plan.market, *plan.books, planned_quantity=plan.quantity,
                              fee_per_pair=sum(plan.fee_per_share), safety_cushion=Decimal('0.02'))
            if pair is None:
                reason = 'no_quote_pair'
            elif not can_open_market(startup_cap=startup_cap, occupied=Decimal('0'),
                    planned_shares=plan.quantity, projected_market_unpaired=plan.quantity,
                    current_unpaired_cost=Decimal('0'),
                    proposed_unpaired_cost=max((q.price+fee)*q.quantity for q, fee in zip(pair, plan.fee_per_share))):
                reason = 'capital_limit'
        candidates.append(Candidate(plan.market.condition_id, score_market(plan.market).score, plan, reason))
    return Ranking(tuple(sorted(candidates, key=lambda r: (-r.score, r.condition_id))))


@dataclass(frozen=True, slots=True)
class RotationDecision:
    action: str
    target: str | None
    reason: str


def evaluate_rotation(ranking, *, current, phase, complete, current_valid, now, window_end):
    leader = ranking.leader
    if current is not None and current_valid is False:
        return RotationDecision('exit', None, 'current_invalid')
    if window_end is None or now >= window_end:
        return RotationDecision('hold', None, 'window_expired' if current else 'needs_new_on')
    if not complete:
        return RotationDecision('hold', None, 'incomplete_scan')
    if leader is None:
        return RotationDecision('hold', None, 'no_executable_candidate')
    if current is None:
        return RotationDecision('enter', leader.condition_id, 'best_executable_candidate')
    if phase == 'exiting' and current_valid is True and leader.condition_id == current:
        return RotationDecision('resume', current, 'current_leads_again')
    if current is not None:
        active = next((c for c in ranking.candidates if c.condition_id == current), None)
        if active is not None and leader.score > active.score:
            return RotationDecision('exit', leader.condition_id, 'higher_score')
    if current is not None and current_valid is None:
        return RotationDecision('hold', None, 'current_unknown')
    return RotationDecision('hold', None, 'current_leads_or_ties')
