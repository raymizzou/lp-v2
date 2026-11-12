"""In-memory exchange facts with explicit scripts and caller-supplied time."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from lp_v2.models import (
    Balance, Fill, MergeRequest, MergeResult, Order, OrderRequest, OrderResult, OrderStatus,
    Outcome, Position,
)


def _amount(value: Decimal, *, positive: bool) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError("amount must be a finite Decimal in the allowed range")


class FakeSDK:
    def __init__(self, *, balance: Balance, positions: tuple[Position, ...] = ()):
        self._balance = balance
        self._positions = {(p.owner, p.condition_id, p.token_id): p for p in positions}
        self._place_scripts = {}
        self._place_results = {}
        self._orders = {}
        self._trades = {}
        self._cancel_scripts = {}
        self._fill_notifications = []
        self._merge_scripts = {}
        self._merge_results = {(r.request.owner, r.request.merge_id): r for r in balance.merge_results or ()}
        self.readback_skips = set()

    def diagnose_readback_skip(self, kind, identity, reason):
        self.readback_skips.add((kind, identity, reason))

    def script_place(self, request: OrderRequest, outcome: Outcome) -> None:
        self._place_scripts[request] = outcome

    def place_order(self, request: OrderRequest, *, at: datetime) -> OrderResult:
        key = (request.owner, request.request_id)
        if key in self._place_results:
            result = self._place_results[key]
            if result.request != request:
                raise ValueError("request identity reused with different intent")
            return result
        _amount(request.quote.quantity, positive=True)
        _amount(request.quote.price, positive=True)
        if request.quote.price >= 1 or not request.created_at <= at < request.expires_at:
            raise ValueError("invalid order price or lifetime")
        outcome = self._place_scripts.pop(request)
        order = None
        if outcome == Outcome.SUCCESS:
            order = Order(
                order_id=f"order-{len(self._orders) + 1}", request=request,
                filled_quantity=Decimal("0"), remaining_quantity=request.quote.quantity,
                status=OrderStatus.LIVE, observed_at=at, authoritative=True,
            )
            self._orders[order.order_id] = order
        result = OrderResult(request, outcome, order, at)
        self._place_results[key] = result
        return result

    def resolve_place(self, request: OrderRequest, outcome: Outcome, *, at: datetime) -> OrderResult:
        """Script an authoritative lookup result, not another placement attempt."""
        key = (request.owner, request.request_id)
        previous = self._place_results[key]
        if previous.request != request:
            raise ValueError("request identity reused with different intent")
        if outcome not in (Outcome.SUCCESS, Outcome.FAILED) or at < previous.observed_at:
            raise ValueError("resolution must be definitive and not precede submission")
        if previous.outcome != Outcome.UNKNOWN:
            if previous.outcome != outcome:
                raise ValueError("resolution contradicts established placement")
            return previous
        order = None
        if outcome == Outcome.SUCCESS:
            order = Order(f"order-{len(self._orders) + 1}", request, Decimal("0"), request.quote.quantity,
                          OrderStatus.LIVE, at, True)
            self._orders[order.order_id] = order
        result = OrderResult(request, outcome, order, at)
        self._place_results[key] = result
        return result

    def read_orders(self) -> tuple[Order | OrderResult, ...]:
        unbound = tuple(r for r in self._place_results.values() if r.order is None)
        return (*self._orders.values(), *unbound)

    def script_cancel(self, order_id: str, outcome: Outcome, *, cancelled: bool) -> None:
        if (outcome == Outcome.SUCCESS and not cancelled) or (outcome == Outcome.FAILED and cancelled):
            raise ValueError("cancellation result contradicts exchange fact")
        self._cancel_scripts[order_id] = (outcome, cancelled)

    def cancel_order(self, order_id: str, *, at: datetime) -> OrderResult:
        order = self._orders[order_id]
        outcome, cancelled = self._cancel_scripts.pop(order_id)
        if at < order.observed_at:
            raise ValueError("cancellation precedes exchange fact")
        order = replace(order, cancel_requested_at=order.cancel_requested_at or at)
        before = order
        if cancelled and order.status == OrderStatus.LIVE:
            order = replace(order, remaining_quantity=Decimal("0"), status=OrderStatus.CANCELLED,
                            observed_at=at)
        self._orders[order_id] = order
        if outcome == Outcome.UNKNOWN:
            order = replace(before, authoritative=False, observed_at=at)
        return OrderResult(order.request, outcome, order, at)

    def execute_fill(self, order_id: str, *, trade_id: str, quantity: Decimal,
                     price: Decimal, fee_usdc: Decimal, at: datetime, deliver: bool = True) -> Fill:
        order = self._orders[order_id]
        request = order.request
        quote = request.quote
        fill = Fill(trade_id, order_id, request.request_id, request.owner, quote.condition_id,
                    quote.token_id, quantity, price, fee_usdc, at)
        if trade_id in self._trades:
            if self._trades[trade_id] != fill:
                raise ValueError("trade identity reused with different execution")
            return self._trades[trade_id]
        _amount(quantity, positive=True)
        _amount(price, positive=True)
        _amount(fee_usdc, positive=False)
        if order.status != OrderStatus.LIVE or quantity > order.remaining_quantity:
            raise ValueError("fill exceeds live order residual")
        if price > quote.price or not request.created_at <= at < request.expires_at:
            raise ValueError("fill violates order price or lifetime")
        cost = quantity * price + fee_usdc
        if cost > self._balance.usdc:
            raise ValueError("insufficient wallet balance")
        key = (request.owner, quote.condition_id, quote.token_id)
        position = self._positions.get(key, Position(*key, Decimal("0"), Decimal("0"), at))
        remaining = order.remaining_quantity - quantity
        self._orders[order_id] = replace(
            order, filled_quantity=order.filled_quantity + quantity,
            remaining_quantity=remaining, observed_at=at,
            status=OrderStatus.FILLED if remaining == 0 else OrderStatus.LIVE,
        )
        self._positions[key] = replace(position, quantity=position.quantity + quantity,
                                       cost_usdc=position.cost_usdc + cost, updated_at=at)
        self._balance = replace(self._balance, usdc=self._balance.usdc - cost, observed_at=at)
        self._trades[trade_id] = fill
        if deliver:
            self.deliver_fill(trade_id)
        return fill

    def deliver_fill(self, trade_id: str) -> None:
        self._fill_notifications.append(self._trades[trade_id])

    def take_fill_notifications(self) -> tuple[Fill, ...]:
        notifications = tuple(self._fill_notifications)
        self._fill_notifications.clear()
        return notifications

    def read_trades(self) -> tuple[Fill, ...]:
        return tuple(self._trades.values())

    def read_positions(self) -> tuple[Position, ...]:
        return tuple(self._positions.values())

    def read_balance(self) -> Balance:
        return replace(self._balance, merge_results=tuple(self._merge_results.values()))

    def script_wallet_balance(self, amount: Decimal, *, at: datetime) -> None:
        _amount(amount, positive=False)
        self._balance = replace(self._balance, usdc=amount, observed_at=at)

    def script_merge(self, request: MergeRequest, outcome: Outcome) -> None:
        self._merge_scripts[request] = outcome

    def merge(self, request: MergeRequest, *, at: datetime) -> MergeResult:
        key = (request.owner, request.merge_id)
        if key in self._merge_results:
            result = self._merge_results[key]
            if result.request != request:
                raise ValueError("merge identity reused with different intent")
            return result
        _amount(request.quantity, positive=True)
        if request.yes_token_id == request.no_token_id or at < request.created_at:
            raise ValueError("invalid merge pair or time")
        legs = [self._positions.get((request.owner, request.condition_id, token))
                for token in (request.yes_token_id, request.no_token_id)]
        if any(leg is None or leg.quantity < request.quantity for leg in legs):
            raise ValueError("insufficient owned pair inventory")
        outcome = self._merge_scripts.pop(request)
        result = MergeResult(request, outcome, Decimal("0"), at)
        self._merge_results[key] = result
        return result

    def receive_merge(self, request: MergeRequest, *, at: datetime) -> MergeResult:
        key = (request.owner, request.merge_id)
        result = self._merge_results[key]
        if result.request != request:
            raise ValueError("merge identity reused with different intent")
        if result.outcome != Outcome.SUCCESS:
            raise ValueError("receipt requires a successful acknowledgement")
        if result.received_usdc != 0:
            return result
        if at < result.observed_at:
            raise ValueError("receipt precedes acknowledgement")
        keys = [(request.owner, request.condition_id, token)
                for token in (request.yes_token_id, request.no_token_id)]
        legs = [self._positions[position_key] for position_key in keys]
        if any(leg.quantity < request.quantity for leg in legs):
            raise ValueError("insufficient owned pair inventory")
        consumed = []
        for position_key, position in zip(keys, legs):
            remaining = position.quantity - request.quantity
            cost = position.cost_usdc if remaining == 0 else position.cost_usdc * request.quantity / position.quantity
            consumed.append(replace(position, quantity=request.quantity, cost_usdc=cost, updated_at=at))
            self._positions[position_key] = replace(
                position, quantity=remaining,
                cost_usdc=position.cost_usdc - cost, updated_at=at,
            )
        result = replace(result, received_usdc=request.quantity, observed_at=at, consumed_positions=tuple(consumed))
        self._balance = replace(self._balance, usdc=self._balance.usdc + request.quantity, observed_at=at)
        self._merge_results[key] = result
        return result

    def resolve_merge(self, request: MergeRequest, outcome: Outcome, *, at: datetime) -> MergeResult:
        """Script a definitive lookup for an existing uncertain attempt."""
        key = (request.owner, request.merge_id)
        previous = self._merge_results[key]
        if previous.request != request:
            raise ValueError("merge identity reused with different intent")
        if outcome not in (Outcome.SUCCESS, Outcome.FAILED) or at < previous.observed_at:
            raise ValueError("resolution must be definitive and not precede submission")
        if previous.outcome != Outcome.UNKNOWN:
            if previous.outcome != outcome:
                raise ValueError("resolution contradicts established merge result")
            return previous
        result = replace(previous, outcome=outcome, observed_at=at)
        self._merge_results[key] = result
        return result
