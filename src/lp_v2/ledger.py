"""In-memory owned facts; execution and persistence belong outside the ledger."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from lp_v2.capital import can_open_market, capital_breakdown, capital_exposure
from lp_v2.models import (
    Balance, CancelRequest, CapitalReservation, Fill, LedgerSnapshot, MergeRequest, MergeResult, Order, OrderRequest, OrderResult,
    OrderStatus, Outcome, Position, ReservationTerms,
)
from lp_v2.sdk_port import SDKPort


def _money(*values: Decimal) -> None:
    if any(not isinstance(v, Decimal) or not v.is_finite() or v < 0 for v in values):
        raise ValueError("ledger amounts must be finite nonnegative Decimals")


def _order_priority(order: Order):
    terminal = order.authoritative and order.status in (OrderStatus.CANCELLED, OrderStatus.FILLED)
    return terminal, order.authoritative, order.observed_at, order.filled_quantity


def _canonical_order(order, fills):
    quantity = max(order.filled_quantity, sum((f.quantity for f in fills), Decimal("0")))
    terminal = order.authoritative and order.status in (OrderStatus.CANCELLED, OrderStatus.FILLED)
    remaining = Decimal("0") if terminal else min(order.remaining_quantity, order.request.quote.quantity - quantity)
    status = order.status if terminal else OrderStatus.FILLED if quantity == order.request.quote.quantity else OrderStatus.LIVE
    observed = max([order.observed_at, *(f.executed_at for f in fills)])
    return Order(order.order_id, order.request, quantity, remaining, status, observed,
                 order.authoritative, order.cancel_requested_at)


def _terms_valid(terms: ReservationTerms) -> None:
    _money(terms.planned_quantity, *terms.fee_per_share)
    if (len(terms.token_ids) != 2 or len(set(terms.token_ids)) != 2 or not all(terms.token_ids)
            or len(terms.fee_per_share) != 2 or type(terms.generation) is not int or terms.generation < 0):
        raise ValueError("invalid reservation terms")


def _request_valid(request: OrderRequest) -> None:
    terms = request.reservation_terms
    if terms is None:
        raise ValueError("request lacks reservation provenance")
    _terms_valid(terms)
    _money(request.quote.quantity, request.quote.price)
    if (not request.request_id or not request.quote.condition_id or request.quote.token_id not in terms.token_ids
            or request.quote.quantity == 0 or not 0 < request.quote.price < 1
            or request.created_at >= request.expires_at):
        raise ValueError("invalid order intent")


def _merge_valid(request: MergeRequest, owner: str, terms: ReservationTerms | None) -> None:
    _money(request.quantity)
    if (not request.merge_id or request.owner != owner or terms is None
            or (request.yes_token_id, request.no_token_id) != terms.token_ids
            or request.generation != terms.generation or request.quantity == 0):
        raise ValueError("merge intent does not match reservation")


class Ledger:
    def __init__(self, owner: str, startup_cap: Decimal, *, balance: Balance | None = None):
        _money(startup_cap)
        if not owner or startup_cap == 0:
            raise ValueError("stable owner and positive startup cap are required")
        self.owner = owner
        self.startup_cap = startup_cap
        if balance is not None:
            _money(balance.usdc)
        self._cash = balance.usdc if balance is not None else None
        self._terms = {}
        self._requests = {}
        self._order_facts = {}
        self._fills = {}
        self._rejected = {}
        self._merge_requests = {}
        self._merge_facts = {}
        self._settled = {}
        self._cancels = {}

    def reserve_market(self, condition_id: str, terms: ReservationTerms, *,
                       projected_market_unpaired: Decimal, current_unpaired_cost: Decimal,
                       proposed_unpaired_cost: Decimal) -> bool:
        _terms_valid(terms)
        if not condition_id:
            raise ValueError("invalid reservation terms")
        previous = self._terms.get(condition_id)
        if previous is not None and previous.planned_quantity > 0:
            return False
        generation = previous.generation + 1 if previous is not None else 0
        if terms.generation != generation:
            raise ValueError("reservation generation mismatch")
        if not can_open_market(startup_cap=self.startup_cap, occupied=self.snapshot().capital.total,
                               planned_shares=terms.planned_quantity, projected_market_unpaired=projected_market_unpaired,
                               current_unpaired_cost=current_unpaired_cost, proposed_unpaired_cost=proposed_unpaired_cost):
            return False
        self._terms[condition_id] = terms
        return True

    def reservation_terms(self, condition_id: str) -> ReservationTerms:
        return self._terms[condition_id]

    def undelivered_quantity(self, condition_id: str, token_id: str) -> Decimal:
        """Matched cumulative quantity whose fee-inclusive trade notice is still missing."""
        return sum((o.filled_quantity - sum((f.quantity for f in self._fills.values() if f.order_id == o.order_id), Decimal("0"))
                    for o in self._canonical_orders() if self._active(o.request)
                    and o.request.quote.condition_id == condition_id and o.request.quote.token_id == token_id), Decimal("0"))

    def oldest_holding(self, condition_id: str) -> datetime | None:
        events = [(f.executed_at, 0, f.token_id, f.quantity) for f in self._fills.values() if f.condition_id == condition_id]
        events.extend((r.observed_at, 1, p.token_id, -p.quantity) for r in self._settled.values()
                      if r.request.condition_id == condition_id for p in r.consumed_positions)
        quantities, opened = {}, {}
        for at, _, token, change in sorted(events):
            previous = quantities.get(token, Decimal("0"))
            if change > 0 and previous == 0:
                opened[token] = at
            quantities[token] = previous + change
            if quantities[token] < 0:
                raise ValueError("settlement time precedes owned inventory")
            if quantities[token] == 0:
                opened.pop(token, None)
        return min(opened.values(), default=None)

    def register_cancel(self, request: CancelRequest) -> bool:
        order = next((o for o in self._canonical_orders() if o.order_id == request.order_id), None)
        if (order is None or request.owner != self.owner or request.request_id != order.request.request_id
                or request.condition_id != order.request.quote.condition_id or request.created_at < order.request.created_at):
            raise ValueError("cancellation identity/time does not match owned order")
        if request.order_id in self._cancels or (order.authoritative and order.status != OrderStatus.LIVE):
            return False
        self._cancels[request.order_id] = request
        return True

    def mergeable_quantity(self, condition_id: str) -> Decimal:
        terms = self._terms[condition_id]
        positions = {p.token_id: p.quantity for p in self._positions() if p.condition_id == condition_id}
        reserved = sum((r.quantity for key, r in self._merge_requests.items()
                        if r.condition_id == condition_id and {r.yes_token_id, r.no_token_id} == set(terms.token_ids)
                        and key not in self._settled
                        and (key not in self._merge_facts or self._merge_facts[key].outcome != Outcome.FAILED)), Decimal("0"))
        return max(Decimal("0"), min(positions.get(token, Decimal("0")) for token in terms.token_ids) - reserved)

    def register_merge(self, request: MergeRequest) -> bool:
        if request.merge_id in self._merge_requests:
            if self._merge_requests[request.merge_id] != request:
                raise ValueError("merge identity has changed payload")
            return False
        terms = self._terms.get(request.condition_id)
        _merge_valid(request, self.owner, terms)
        if request.quantity > self.mergeable_quantity(request.condition_id):
            raise ValueError("merge exceeds unreserved owned pair inventory")
        self._merge_requests[request.merge_id] = request
        return True

    def apply_merge(self, result: MergeResult) -> None:
        request = result.request
        if request.owner != self.owner:
            return
        if self._merge_requests.get(request.merge_id) != request:
            raise ValueError("merge identity has changed payload or is unregistered")
        _money(result.received_usdc)
        if result.received_usdc:
            if result.outcome != Outcome.SUCCESS or result.received_usdc != request.quantity:
                raise ValueError("receipt must confirm the exact successful merge quantity")
            legs = result.consumed_positions
            if (len(legs) != 2 or {p.token_id for p in legs} != {request.yes_token_id, request.no_token_id}
                    or any(p.owner != self.owner or p.condition_id != request.condition_id
                           or p.quantity != request.quantity for p in legs)):
                raise ValueError("receipt lacks matching consumed-position provenance")
            for position in legs:
                _money(position.quantity, position.cost_usdc)
        elif result.consumed_positions:
            raise ValueError("acknowledgement cannot consume inventory")
        previous = self._merge_facts.get(request.merge_id)
        if previous is not None and previous.outcome == Outcome.FAILED:
            if result.received_usdc:
                raise ValueError("receipt contradicts definitive merge failure")
            return
        if previous is not None and previous.received_usdc:
            if result.received_usdc and result != previous:
                raise ValueError("merge receipt identity has changed payload")
            return
        if (previous is None or result.received_usdc or result.outcome == Outcome.FAILED
                or (result.outcome != Outcome.UNKNOWN and previous.outcome == Outcome.UNKNOWN)
                or (result.outcome == previous.outcome and result.observed_at > previous.observed_at)):
            self._merge_facts[request.merge_id] = result

    def reconcile(self, sdk: SDKPort) -> None:
        """Adopt a consistent fact readback atomically, retaining unseen pending intents.

        Recovery requires complete owned history and authentic request/receipt
        provenance. Missing proof raises; a wallet delta is never a merge receipt.
        """
        orders, trades = sdk.read_orders(), sdk.read_trades()
        positions, balance = sdk.read_positions(), sdk.read_balance()
        candidate = deepcopy(self)
        _money(balance.usdc)
        if balance.merge_results is None:
            raise ValueError("complete merge history proof is unavailable")
        provenance = {(r.quote.condition_id, r.reservation_terms.generation): r.reservation_terms
                      for r in candidate._requests.values()}
        for order in orders:
            request = order.request
            if request.owner != self.owner:
                continue
            _request_valid(request)
            previous = candidate._requests.get(request.request_id)
            if previous is not None and previous != request:
                raise ValueError("request identity has changed payload")
            terms = request.reservation_terms
            cid = request.quote.condition_id
            old = provenance.setdefault((cid, terms.generation), terms)
            if old.token_ids != terms.token_ids or old.fee_per_share != terms.fee_per_share:
                raise ValueError("conflicting reservation provenance")
            current = candidate._terms.get(cid)
            if (current is None or terms.generation > current.generation
                    or (terms.generation == current.generation and terms.planned_quantity < current.planned_quantity)):
                candidate._terms[cid] = terms
            candidate._requests[request.request_id] = request
        # Validate every fact, then refresh the complete batch once on the atomic candidate.
        candidate._apply_orders(order if isinstance(order, OrderResult)
                                else OrderResult(order.request, Outcome.SUCCESS, order, order.observed_at)
                                for order in orders)
        for fill in sorted(trades, key=lambda f: (f.executed_at, f.trade_id)):
            candidate.apply_fill(fill)
        for result in balance.merge_results:
            if result.request.owner == self.owner:
                request = result.request
                if request.merge_id not in candidate._merge_requests:
                    terms = next((r.reservation_terms for r in candidate._requests.values()
                                  if r.quote.condition_id == request.condition_id
                                  and r.reservation_terms.generation == request.generation), None)
                    _merge_valid(request, self.owner, terms)
                    candidate._merge_requests[request.merge_id] = request
                candidate.apply_merge(result)
                if result.received_usdc:
                    candidate._settled[request.merge_id] = result
        actual = {}
        for position in positions:
            if position.owner != self.owner:
                continue
            _money(position.quantity, position.cost_usdc)
            key = (position.condition_id, position.token_id)
            if key in actual and actual[key] != (position.quantity, position.cost_usdc):
                raise ValueError("conflicting position readback")
            actual[key] = (position.quantity, position.cost_usdc)
        expected = {(p.condition_id, p.token_id): (p.quantity, p.cost_usdc) for p in candidate._positions()}
        if {k: v for k, v in actual.items() if any(v)} != {k: v for k, v in expected.items() if any(v)}:
            raise ValueError("position readback does not reconcile with trade/receipt proof")
        candidate._check_historical_exposure()
        candidate._cash = balance.usdc
        candidate._refresh()
        self.__dict__.update(candidate.__dict__)

    def register_order(self, request: OrderRequest) -> bool:
        if request.request_id in self._requests:
            if self._requests[request.request_id] != request:
                raise ValueError("request identity has changed payload")
            return False
        if request.owner != self.owner or not request.request_id:
            raise ValueError("request owner/identity mismatch")
        _request_valid(request)
        terms = self._terms.get(request.quote.condition_id)
        if request.reservation_terms != terms or terms is None or request.quote.token_id not in terms.token_ids:
            raise ValueError("request lacks matching reservation provenance")
        candidate = deepcopy(self)
        candidate._requests[request.request_id] = request
        reservations, _, capital = candidate._capital_view(request.quote.condition_id)
        if reservations[0].planned_quantity > terms.planned_quantity or capital.total > terms.planned_quantity:
            raise ValueError("order exceeds admitted market reservation")
        self._requests[request.request_id] = request
        self._refresh()
        return True

    def apply_order(self, result: OrderResult) -> None:
        self._apply_orders((result,))

    def _apply_orders(self, results) -> None:
        results = tuple(r for r in results if r.request.owner == self.owner)
        if not results:
            return
        orders = {o.order_id: o for o in self._canonical_orders()}
        request_orders = {o.request.request_id: o.order_id for o in orders.values()}
        fills = {}
        for fill in self._fills.values():
            fills.setdefault(fill.order_id, []).append(fill)
        changed = False
        for result in results:
            if self._requests.get(result.request.request_id) != result.request:
                raise ValueError("request identity has changed payload or is unregistered")
            order = result.order
            if order is None:
                if result.outcome == Outcome.FAILED:
                    if result.request.request_id in request_orders:
                        raise ValueError("placement rejection contradicts an existing order")
                    previous = self._rejected.get(result.request.request_id)
                    if previous is None or result.observed_at > previous.observed_at:
                        self._rejected[result.request.request_id] = result
                    changed = True
                continue
            if result.request.request_id in self._rejected:
                raise ValueError("order contradicts confirmed placement rejection")
            if order.request != result.request or not order.order_id:
                raise ValueError("order identity does not match request")
            if order.cancel_requested_at is not None and order.cancel_requested_at < order.request.created_at:
                raise ValueError("cancellation attempt precedes order intent")
            _money(order.filled_quantity, order.remaining_quantity)
            terminal = order.authoritative and order.status in (OrderStatus.CANCELLED, OrderStatus.FILLED)
            if (order.status not in (OrderStatus.LIVE, OrderStatus.CANCELLED, OrderStatus.FILLED)
                    or (terminal and order.remaining_quantity != 0)
                    or (order.authoritative and order.status == OrderStatus.FILLED
                        and order.filled_quantity != order.request.quote.quantity)):
                raise ValueError("invalid terminal order quantities/status")
            if order.filled_quantity + order.remaining_quantity > order.request.quote.quantity:
                raise ValueError("order quantities exceed intent")
            previous = self._order_facts.get(order.order_id)
            if previous is not None and previous.request != order.request:
                raise ValueError("order identity has changed payload")
            if (order.request.request_id in request_orders
                    and request_orders[order.request.request_id] != order.order_id):
                raise ValueError("request identity bound to another order")
            current = orders.get(order.order_id)
            if current is not None:
                if ((terminal and order.filled_quantity < current.filled_quantity)
                        or (current.authoritative and current.status != OrderStatus.LIVE
                            and order.filled_quantity > current.filled_quantity)):
                    raise ValueError("terminal cumulative quantity contradicts recorded fills")
            chosen = order if previous is None or _order_priority(order) > _order_priority(previous) else previous
            quantity = max(order.filled_quantity, previous.filled_quantity if previous else Decimal("0"))
            remaining = Decimal("0") if chosen.authoritative and chosen.status != OrderStatus.LIVE else chosen.request.quote.quantity - quantity
            self._order_facts[order.order_id] = replace(chosen, filled_quantity=quantity, remaining_quantity=remaining)
            if order.cancel_requested_at is not None:
                self._cancels.setdefault(order.order_id, CancelRequest(order.order_id, order.request.request_id, self.owner,
                                                                      order.request.quote.condition_id, order.cancel_requested_at))
            orders[order.order_id] = _canonical_order(self._order_facts[order.order_id], fills.get(order.order_id, ()))
            request_orders[order.request.request_id] = order.order_id
            changed = True
        if changed:
            self._refresh()

    def apply_fill(self, fill: Fill) -> None:
        if fill.owner != self.owner:
            return
        if fill.trade_id in self._fills:
            if self._fills[fill.trade_id] != fill:
                raise ValueError("trade identity has changed payload")
            return
        request = self._requests.get(fill.request_id)
        if fill.request_id in self._rejected:
            raise ValueError("fill contradicts confirmed placement rejection")
        if (request is None or fill.condition_id != request.quote.condition_id
                or fill.token_id != request.quote.token_id or not fill.trade_id or not fill.order_id):
            raise ValueError("fill identity does not match registered intent")
        _money(fill.quantity, fill.price, fill.fee_usdc)
        if fill.quantity == 0 or not 0 < fill.price <= request.quote.price:
            raise ValueError("invalid fill amount or price")
        for current in self._canonical_orders():
            if ((current.order_id == fill.order_id and current.request != request)
                    or (current.request.request_id == fill.request_id and current.order_id != fill.order_id)):
                raise ValueError("fill order/request identity conflict")
        quantity = sum((f.quantity for f in self._fills.values() if f.order_id == fill.order_id), fill.quantity)
        if quantity > request.quote.quantity:
            raise ValueError("fills exceed original quantity")
        fact = self._order_facts.get(fill.order_id)
        if fact is not None and fact.authoritative and fact.status != OrderStatus.LIVE and quantity > fact.filled_quantity:
            raise ValueError("fill exceeds terminal cumulative quantity")
        self._fills[fill.trade_id] = fill
        if self._cash is not None:
            self._cash -= fill.quantity * fill.price + fill.fee_usdc
        self._refresh()

    def _canonical_orders(self) -> tuple[Order, ...]:
        orders = dict(self._order_facts)
        fills_by_order = {}
        for fill in self._fills.values():
            fills_by_order.setdefault(fill.order_id, []).append(fill)
            if fill.order_id not in orders:
                request = self._requests[fill.request_id]
                orders[fill.order_id] = Order(fill.order_id, request, Decimal("0"), request.quote.quantity,
                                              OrderStatus.LIVE, request.created_at, False)
        return tuple(_canonical_order(order, fills_by_order.get(order_id, ()))
                     for order_id, order in sorted(orders.items()))

    def _positions(self) -> tuple[Position, ...]:
        positions = {}
        events = [(f.executed_at, 0, f.trade_id, f) for f in self._fills.values()]
        events.extend((r.observed_at, 1, r.request.merge_id, r) for r in self._settled.values())
        # Preserve execution arithmetic across partial settlements and later fills.
        for at, _, _, fact in sorted(events):
            if isinstance(fact, Fill):
                key = (fact.condition_id, fact.token_id)
                quantity, cost, updated = positions.get(key, (Decimal("0"), Decimal("0"), at))
                positions[key] = (quantity + fact.quantity, cost + (fact.quantity * fact.price + fact.fee_usdc),
                                  max(updated, at))
                continue
            for consumed in fact.consumed_positions:
                key = (consumed.condition_id, consumed.token_id)
                previous = positions.get(key)
                if previous is None or previous[0] < consumed.quantity or previous[1] < consumed.cost_usdc:
                    raise ValueError("receipt exceeds recorded owned inventory")
                quantity, cost, updated = previous
                positions[key] = (quantity - consumed.quantity, cost - consumed.cost_usdc, max(updated, at))
        return tuple(Position(self.owner, *key, *positions[key]) for key in sorted(positions))

    def _active(self, request: OrderRequest) -> bool:
        return request.reservation_terms.generation == self._terms[request.quote.condition_id].generation

    def _check_historical_exposure(self) -> None:
        orders = {o.request.request_id: o for o in self._canonical_orders()}
        remaining = {}
        for key, request in self._requests.items():
            if self._active(request) or key in self._rejected:
                continue
            order = orders.get(key)
            if order is None or not order.authoritative or order.status == OrderStatus.LIVE:
                raise ValueError("older generation has possibly live or unresolved orders")
            identity = (request.quote.condition_id, request.reservation_terms.generation, request.quote.token_id)
            remaining[identity] = remaining.get(identity, Decimal("0")) + order.filled_quantity
        for key, request in self._merge_requests.items():
            if request.generation == self._terms[request.condition_id].generation:
                continue
            if key not in self._settled:
                if key not in self._merge_facts or self._merge_facts[key].outcome != Outcome.FAILED:
                    raise ValueError("older generation has an unresolved merge")
                continue
            for token in (request.yes_token_id, request.no_token_id):
                identity = (request.condition_id, request.generation, token)
                remaining[identity] = remaining.get(identity, Decimal("0")) - request.quantity
        if any(remaining.values()):
            raise ValueError("older generation inventory is not fully settled")

    def _capital_view(self, condition_id: str | None = None, *, orders=None, positions=None):
        markets = self._terms if condition_id is None else {condition_id: self._terms[condition_id]}
        orders = self._canonical_orders() if orders is None else orders
        positions = self._positions() if positions is None else positions
        orders = tuple(o for o in orders if o.request.quote.condition_id in markets and self._active(o.request))
        known_ids = {o.request.request_id for o in orders}
        pending = tuple(r for key, r in sorted(self._requests.items())
                        if r.quote.condition_id in markets and self._active(r) and key not in known_ids and key not in self._rejected)
        positions = tuple(p for p in positions if p.condition_id in markets)
        reservations = tuple(CapitalReservation(
            self.owner, cid, terms.token_ids, terms.planned_quantity,
            terms.planned_quantity * sum(terms.fee_per_share),
            tuple(key for key, r in sorted(self._requests.items()) if r.quote.condition_id == cid and self._active(r)),
            tuple(r for key, r in sorted(self._merge_requests.items())
                  if r.condition_id == cid and r.generation == terms.generation),
        ) for cid, terms in sorted(markets.items()))
        receipts = tuple(r for key, r in sorted(self._settled.items())
                         if r.request.condition_id in markets and r.request.generation == markets[r.request.condition_id].generation)
        args = dict(positions=positions, orders=orders, pending=pending, merge_results=receipts,
                    request_results=tuple(r for r in self._rejected.values()
                                          if r.request.quote.condition_id in markets and self._active(r.request)))
        _, exposures = capital_exposure(self.owner, reservations=reservations, **args)
        exposure = {e.condition_id: e for e in exposures}
        reservations = tuple(replace(r, planned_quantity=exposure[r.condition_id].reserved_quantity,
                                     remaining_fee_usdc=sum((q * fee for q, fee in zip(exposure[r.condition_id].fee_obligations,
                                                                                   self._terms[r.condition_id].fee_per_share)), Decimal("0")))
                             for r in reservations)
        capital = capital_breakdown(self.owner, reservations=reservations, **args)
        return reservations, pending, capital

    def _refresh(self) -> None:
        reservations, _, _ = self._capital_view()
        for reservation in reservations:
            terms = self._terms[reservation.condition_id]
            if reservation.planned_quantity < terms.planned_quantity:
                self._terms[reservation.condition_id] = replace(terms, planned_quantity=reservation.planned_quantity)

    def snapshot(self) -> LedgerSnapshot:
        orders, positions = self._canonical_orders(), self._positions()
        reservations, pending, capital = self._capital_view(orders=orders, positions=positions)
        return LedgerSnapshot(self.owner, self.startup_cap, tuple(self._requests[key] for key in sorted(self._requests)),
                              orders, tuple(self._fills[key] for key in sorted(self._fills)), positions,
                              pending, tuple(self._rejected[key] for key in sorted(self._rejected)),
                              tuple(self._merge_facts[key] for key in sorted(self._merge_facts)),
                              reservations, self._cash, capital,
                              sum((r.received_usdc - sum((p.cost_usdc for p in r.consumed_positions), Decimal("0"))
                                   for r in self._settled.values()), Decimal("0")),
                              tuple(self._cancels[key] for key in sorted(self._cancels)))
