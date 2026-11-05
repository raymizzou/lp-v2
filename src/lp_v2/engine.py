"""One synchronous decision round; callers execute the returned intents."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from lp_v2.capital import projected_unpaired_cost
from lp_v2.hedge import hedge_quote
from lp_v2.ledger import Ledger
from lp_v2.models import CancelRequest, Decision, MarketPlan, MergeRequest, OrderRequest, OrderStatus, Position, Quote, ReservationTerms
from lp_v2.quoting import quote_pair
from lp_v2.scoring import eligible_market, score_market


class Engine:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self._ranked_at = None
        self._ranked_ids = ()

    def decide(self, plans: Sequence[MarketPlan], *, now: datetime, allow_open: bool = True) -> Decision:
        if len({p.market.condition_id for p in plans}) != len(plans):
            raise ValueError("duplicate market plans")
        if self._ranked_at is None or now - self._ranked_at >= timedelta(seconds=600):
            ranked = sorted((p for p in plans if eligible_market(p.market)),
                            key=lambda p: (-score_market(p.market).score, p.market.condition_id))
            self._ranked_ids = tuple(p.market.condition_id for p in ranked)
            self._ranked_at = now
        places, cancels, merges = [], [], []
        state = self.ledger.snapshot()
        identities = {r.request_id for r in state.requests}
        for plan in plans:
            cid = plan.market.condition_id
            reservation = next((r for r in state.reservations if r.condition_id == cid), None)
            if reservation is None or reservation.planned_quantity == 0:
                continue
            positions = {p.token_id: p for p in state.positions if p.condition_id == cid}
            cancel_ids = {c.order_id for c in state.cancels}
            for order in state.orders:
                if (order.request.quote.condition_id != cid or order.order_id in cancel_ids
                        or (order.authoritative and order.status != OrderStatus.LIVE)
                        or (now - order.request.created_at < timedelta(seconds=60)
                            and not (not allow_open and order.request.purpose == 'opening'))):
                    continue
                if (not allow_open and order.request.purpose == 'opening') or self._needs_cancel(order, plan, state, now):
                    cancel = CancelRequest(order.order_id, order.request.request_id, state.owner, cid, now)
                    if self.ledger.register_cancel(cancel):
                        cancels.append(cancel)
                        state = self.ledger.snapshot()
            if all(token in positions and positions[token].quantity > 0 for token in reservation.token_ids):
                quantity = self.ledger.mergeable_quantity(cid)
                count = sum(f.condition_id == cid for f in state.fills)
                identity = f"{state.owner}:{cid[:8]}:merge:{cid}:{count}"
                if quantity > 0 and all(r.merge_id != identity for r in reservation.merge_requests):
                    terms = self.ledger.reservation_terms(cid)
                    merge = MergeRequest(identity, state.owner, cid, *terms.token_ids, quantity, now, terms.generation)
                    if self.ledger.register_merge(merge):
                        merges.append(merge)
                        state = self.ledger.snapshot()
            for index, token in enumerate(plan.market.tokens):
                held = positions.get(token.token_id, Position(state.owner, cid, token.token_id, Decimal("0"), Decimal("0"), now))
                opposite_token = plan.market.tokens[1 - index].token_id
                opposite = positions.get(opposite_token, Position(state.owner, cid, opposite_token, Decimal("0"), Decimal("0"), now))
                if held.quantity <= opposite.quantity:
                    continue
                quote = hedge_quote(plan.market, held, opposite, plan.books[1 - index], orders=state.orders,
                                    pending=state.pending, safety_cushion=Decimal("0.02"),
                                    remaining_fee_per_pair=plan.fee_per_share[1 - index])
                if quote is not None:
                    quantity = quote.quantity - self.ledger.undelivered_quantity(cid, opposite_token)
                    if quantity > 0 and quantity >= plan.market.min_order_size:
                        places.append(self._place(replace(quote, quantity=quantity), now, identities, purpose="hedge"))
                        state = self.ledger.snapshot()
        current = {p.market.condition_id: p for p in plans}
        ranked = (current[cid] for cid in self._ranked_ids if cid in current and eligible_market(current[cid].market))
        for plan in ranked if allow_open else ():
            cid = plan.market.condition_id
            previous = next((r for r in state.reservations if r.condition_id == cid), None)
            if previous is not None and previous.planned_quantity > 0:
                continue
            oldest = self.ledger.oldest_holding(cid)
            if oldest is not None and now - oldest > timedelta(hours=24):
                continue
            pair = quote_pair(plan.market, *plan.books, planned_quantity=plan.quantity,
                              fee_per_pair=sum(plan.fee_per_share), safety_cushion=Decimal("0.02"))
            if pair is None:
                continue
            generation = self.ledger.reservation_terms(cid).generation + 1 if previous else 0
            terms = ReservationTerms(tuple(t.token_id for t in plan.market.tokens), plan.quantity, plan.fee_per_share, generation)
            risk = max((q.price + fee) * q.quantity for q, fee in zip(pair, plan.fee_per_share))
            if self.ledger.reserve_market(cid, terms, projected_market_unpaired=plan.quantity,
                                          current_unpaired_cost=projected_unpaired_cost(state),
                                          proposed_unpaired_cost=risk):
                places.extend(self._place(q, now, identities) for q in pair)
                state = self.ledger.snapshot()
        return Decision(place=tuple(places), cancel=tuple(cancels), merge=tuple(merges))

    @staticmethod
    def _needs_cancel(order, plan, state, now) -> bool:
        quote = order.request.quote
        index = next(i for i, token in enumerate(plan.market.tokens) if token.token_id == quote.token_id)
        book = plan.books[index]
        if quote.price >= book.best_ask or order.request.expires_at - now <= timedelta(seconds=60):
            return True
        opposite_token = plan.market.tokens[1 - index].token_id
        positions = {p.token_id: p for p in state.positions if p.condition_id == quote.condition_id}
        opposite = positions.get(opposite_token)
        costs = [opposite.cost_usdc / opposite.quantity] if opposite is not None and opposite.quantity > 0 else []
        others = [o.request for o in state.orders if o.status == OrderStatus.LIVE or not o.authoritative]
        for request in (*others, *state.pending):
            if request.quote.condition_id == quote.condition_id and request.quote.token_id == opposite_token:
                costs.append(request.quote.price + plan.fee_per_share[1 - index])
        if any(quote.price + plan.fee_per_share[index] + cost > Decimal("0.98") for cost in costs):
            return True
        held_quantity = positions[quote.token_id].quantity if quote.token_id in positions else Decimal("0")
        hedging = order.request.purpose == "hedge" or (opposite is not None and opposite.quantity > held_quantity)
        band = Decimal(str(plan.market.rewards_max_spread)) / 100
        return (not hedging and plan.market.rewards_enabled
                and abs(quote.price - (book.best_bid + book.best_ask) / 2) > band)

    def _place(self, quote: Quote, now: datetime, identities: set[str], *, purpose="opening") -> OrderRequest:
        number = len(identities)
        while f"{self.ledger.owner}:{quote.condition_id[:8]}:order:{number}" in identities:
            number += 1
        request = OrderRequest(f"{self.ledger.owner}:{quote.condition_id[:8]}:order:{number}", self.ledger.owner, quote, now, now + timedelta(hours=1),
                               self.ledger.reservation_terms(quote.condition_id), purpose)
        if self.ledger.register_order(request):
            identities.add(request.request_id)
        return request
