from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lp_v2.capital import recovery_action_allowed
from lp_v2.hedge import hedge_quote
from lp_v2.ledger import Ledger
from lp_v2.models import (
    Balance, Book, CapitalBreakdown, Fill, Market, MergeRequest, Order, OrderRequest, OrderResult, OrderStatus, Outcome,
    Position, Quote, ReservationTerms, Token,
)
from tests.fakes.fake_sdk import FakeSDK


OWNER = "lp-v2:stable-owner"
AT = datetime(2026, 11, 2, tzinfo=UTC)
T1 = AT + timedelta(seconds=1)
T2 = AT + timedelta(seconds=2)
T3 = AT + timedelta(seconds=3)
TERMS = ReservationTerms(("yes", "no"), Decimal("10"), (Decimal("0.01"), Decimal("0.01")))


def prepared_ledger():
    ledger = Ledger(OWNER, Decimal("100"), balance=Balance(Decimal("100"), AT))
    assert ledger.reserve_market("market-1", TERMS, projected_market_unpaired=Decimal("10"),
                                 current_unpaired_cost=Decimal("0"), proposed_unpaired_cost=Decimal("5.60"))
    requests = tuple(OrderRequest(token, OWNER, Quote("market-1", token, Decimal(price), Decimal("10")),
                                  AT, AT + timedelta(hours=1), TERMS)
                     for token, price in (("yes", "0.40"), ("no", "0.55")))
    for request in requests:
        assert ledger.register_order(request) is True
    return ledger, requests


def place_pair(fake, requests):
    results = []
    for request in requests:
        fake.script_place(request, Outcome.SUCCESS)
        results.append(fake.place_order(request, at=AT))
    return tuple(results)


def matched_pair():
    ledger, requests = prepared_ledger()
    fake = FakeSDK(balance=Balance(Decimal("100"), AT))
    for acknowledgement, at in zip(place_pair(fake, requests), (T1, T2)):
        ledger.apply_order(acknowledgement)
        fill = fake.execute_fill(acknowledgement.order.order_id, trade_id=acknowledgement.request.request_id + "-ten",
                                 quantity=Decimal("10"), price=acknowledgement.request.quote.price,
                                 fee_usdc=Decimal("0.10"), at=at)
        ledger.apply_fill(fill)
    return ledger, fake


def test_reconcile_repeated_complete_history_is_stable():
    original,requests=prepared_ledger()
    fake=FakeSDK(balance=Balance(Decimal('100'),AT))
    yes,no=place_pair(fake,requests)
    for result in (yes,no):original.apply_order(result)
    fill=fake.execute_fill(yes.order.order_id,trade_id='partial',quantity=Decimal('4'),price=Decimal('.40'),fee_usdc=Decimal('.04'),at=T1)
    original.apply_fill(fill)
    fake.script_cancel(no.order.order_id,Outcome.SUCCESS,cancelled=True)
    original.apply_order(fake.cancel_order(no.order.order_id,at=T2))
    expected=original.snapshot()
    assert expected.positions==(Position(OWNER,'market-1','yes',Decimal('4'),Decimal('1.64'),T1),)
    assert len(expected.requests)==len(expected.orders)==2 and expected.cash==Decimal('98.36')
    recovered=Ledger(OWNER,Decimal('100'))
    for _ in range(3):
        recovered.reconcile(fake)
        assert recovered.snapshot()==expected
    # Complete cold recovery must reject a new conflicting fact atomically too.
    before=recovered.snapshot()
    fake_facts=fake.read_orders()
    fake.read_orders=lambda:fake_facts+(replace(fake_facts[0],request=replace(requests[0],quote=replace(requests[0].quote,price=Decimal('.41')))),)
    with pytest.raises(ValueError,match='identity'):
        recovered.reconcile(fake)
    assert recovered.snapshot()==before


def test_fill_replay_and_ordering():
    first, requests = prepared_ledger()
    second, same_requests = prepared_ledger()
    assert same_requests == requests
    fake = FakeSDK(balance=Balance(Decimal("100"), AT))
    yes, no = place_pair(fake, requests)
    fill = fake.execute_fill(yes.order.order_id, trade_id="yes-four", quantity=Decimal("4"),
                              price=Decimal("0.40"), fee_usdc=Decimal("0.04"), at=T1)
    snapshots = []
    for ledger, delivery in ((first, (yes, no, fill, fill)), (second, (fill, no, yes, fill))):
        for fact in delivery:
            if isinstance(fact, Fill):
                ledger.apply_fill(fact)
            else:
                ledger.apply_order(fact)
        state = ledger.snapshot()
        assert state.positions == (Position(OWNER, "market-1", "yes", Decimal("4"), Decimal("1.64"), T1),)
        orders = {order.request.quote.token_id: order for order in state.orders}
        assert orders["yes"].filled_quantity == Decimal("4")
        assert orders["yes"].remaining_quantity == Decimal("6")
        assert orders["no"].remaining_quantity == Decimal("10")
        assert state.fills == (Fill("yes-four", yes.order.order_id, "yes", OWNER, "market-1", "yes",
                                   Decimal("4"), Decimal("0.40"), Decimal("0.04"), T1),)
        assert state.pending == ()
        assert state.cash == Decimal("98.36")
        assert state.realized_profit == Decimal("0")
        assert state.capital == CapitalBreakdown(Decimal("1.64"), Decimal("7.90"), Decimal("0"),
                                                Decimal("0.30"), Decimal("0.16"), Decimal("10"))
        with pytest.raises(AttributeError):
            state.cash = Decimal("0")
        with pytest.raises(ValueError, match="trade identity"):
            ledger.apply_fill(replace(fill, fee_usdc=Decimal("0.05")))
        assert ledger.snapshot() == state
        snapshots.append(state)
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("early_cumulative_readback", [False, True])
def test_cancel_unknown_and_late_fill(early_cumulative_readback):
    first, requests = prepared_ledger()
    second, _ = prepared_ledger()
    fake = FakeSDK(balance=Balance(Decimal("100"), AT))
    yes, no = place_pair(fake, requests)
    fill = fake.execute_fill(yes.order.order_id, trade_id="delayed-four", quantity=Decimal("4"),
                              price=Decimal("0.40"), fee_usdc=Decimal("0.04"), at=T1, deliver=False)
    assert fake.take_fill_notifications() == ()
    partial = next(order for order in fake.read_orders() if order.order_id == yes.order.order_id)
    fake.script_cancel(yes.order.order_id, Outcome.UNKNOWN, cancelled=True)
    unknown = fake.cancel_order(yes.order.order_id, at=T2)
    final_yes = next(order for order in fake.read_orders() if order.order_id == yes.order.order_id)
    cancelled_yes = OrderResult(requests[0], Outcome.SUCCESS, final_yes, T2)
    fake.script_cancel(no.order.order_id, Outcome.SUCCESS, cancelled=True)
    cancelled_no = fake.cancel_order(no.order.order_id, at=T3)
    for ledger in (first, second):
        ledger.apply_order(yes)
        ledger.apply_order(no)
        if early_cumulative_readback:
            ledger.apply_order(OrderResult(requests[0], Outcome.SUCCESS, partial, T1))
            stale = replace(yes, outcome=Outcome.UNKNOWN, observed_at=T2,
                            order=replace(yes.order, authoritative=False, observed_at=T2))
            ledger.apply_order(stale)
            current = next(order for order in ledger.snapshot().orders if order.order_id == partial.order_id)
            assert current == partial
        ledger.apply_order(unknown)
        assert ledger.snapshot().capital.total == Decimal("10")
        assert ledger.register_order(requests[0]) is False
    first.apply_order(cancelled_yes)
    first.apply_order(cancelled_no)
    before_delivery = first.snapshot()
    assert before_delivery.capital.total == Decimal("4")
    assert before_delivery.positions == ()
    assert before_delivery.reservations[0].planned_quantity == Decimal("4")
    fake.deliver_fill(fill.trade_id)
    assert fake.take_fill_notifications() == (fill,)
    for fact in (fill, fill, yes, unknown):
        first.apply_fill(fact) if isinstance(fact, Fill) else first.apply_order(fact)
    for fact in (fill, cancelled_no, cancelled_yes, unknown, yes, fill):
        second.apply_fill(fact) if isinstance(fact, Fill) else second.apply_order(fact)
    state = first.snapshot()
    assert state == second.snapshot()
    assert state.positions == (Position(OWNER, "market-1", "yes", Decimal("4"), Decimal("1.64"), T1),)
    assert all(order.status == OrderStatus.CANCELLED and order.authoritative for order in state.orders)
    assert state.capital == CapitalBreakdown(Decimal("1.64"), Decimal("0"), Decimal("0"),
                                            Decimal("2.32"), Decimal("0.04"), Decimal("4"))
    assert state.cash == Decimal("98.36")
    with pytest.raises(ValueError, match="cumulative"):
        first.apply_order(replace(cancelled_yes, order=replace(final_yes, filled_quantity=Decimal("3"), observed_at=T3)))
    assert first.snapshot() == state
    hedge = OrderRequest("hedge-four", OWNER, Quote("market-1", "no", Decimal("0.56"), Decimal("4")),
                         T3, T3 + timedelta(hours=1), first.reservation_terms("market-1"))
    assert first.register_order(hedge) is True
    assert first.snapshot().capital == CapitalBreakdown(Decimal("1.64"), Decimal("0"), Decimal("2.24"),
                                                       Decimal("0.08"), Decimal("0.04"), Decimal("4"))


@pytest.mark.parametrize("scenario", ["success", "failure", "timeout", "unrelated_cash"])
def test_merge_receipt_settles_once(scenario):
    ledger, requests = prepared_ledger()
    fake = FakeSDK(balance=Balance(Decimal("100"), AT))
    yes, no = place_pair(fake, requests)
    ledger.apply_order(yes)
    ledger.apply_order(no)
    yes_fill = fake.execute_fill(yes.order.order_id, trade_id="yes-ten", quantity=Decimal("10"),
                                  price=Decimal("0.40"), fee_usdc=Decimal("0.10"), at=T1)
    no_fill = fake.execute_fill(no.order.order_id, trade_id="no-ten", quantity=Decimal("10"),
                                 price=Decimal("0.55"), fee_usdc=Decimal("0.10"), at=T2)
    ledger.apply_fill(yes_fill)
    ledger.apply_fill(no_fill)
    assert ledger.snapshot().capital.inventory_cost == Decimal("9.70")
    assert ledger.snapshot().cash == fake.read_balance().usdc == Decimal("90.30")
    merge = MergeRequest("merge-ten", OWNER, "market-1", "yes", "no", Decimal("10"), T3)
    assert ledger.register_merge(merge) is True
    outcome = {"failure": Outcome.FAILED, "timeout": Outcome.UNKNOWN}.get(scenario, Outcome.SUCCESS)
    fake.script_merge(merge, outcome)
    acknowledgement = fake.merge(merge, at=T3)
    ledger.apply_merge(acknowledgement)
    assert ledger.snapshot().capital.total == Decimal("10")
    assert ledger.snapshot().realized_profit == Decimal("0")

    assert ledger.register_merge(merge) is False
    if scenario == "unrelated_cash":
        fake.script_wallet_balance(Decimal("150.30"), at=T3)
        ledger.reconcile(fake)
        assert ledger.snapshot().cash == Decimal("150.30")
        assert ledger.snapshot().capital.total == Decimal("10")
        assert ledger.snapshot().realized_profit == Decimal("0")
    elif scenario == "success":
        settled_at = T3 + timedelta(seconds=1)
        receipt = fake.receive_merge(merge, at=settled_at)
        assert receipt.consumed_positions == (
            Position(OWNER, "market-1", "yes", Decimal("10"), Decimal("4.10"), settled_at),
            Position(OWNER, "market-1", "no", Decimal("10"), Decimal("5.60"), settled_at),
        )
        ledger.apply_merge(receipt)
        assert ledger.snapshot().capital.total == Decimal("10")
        assert ledger.snapshot().realized_profit == Decimal("0")
        ledger.reconcile(fake)
        state = ledger.snapshot()
        assert state.cash == Decimal("100.30")
        assert state.capital.total == Decimal("0")
        assert state.realized_profit == Decimal("0.30")
        assert all(p.quantity == 0 and p.cost_usdc == 0 for p in state.positions)
        assert state.reservations[0].planned_quantity == Decimal("0")
        ledger.apply_merge(receipt)
        ledger.apply_merge(acknowledgement)
        ledger.apply_fill(yes_fill)
        ledger.apply_fill(no_fill)
        ledger.reconcile(fake)
        assert ledger.snapshot() == state
        with pytest.raises(ValueError, match="merge identity"):
            ledger.register_merge(replace(merge, quantity=Decimal("9")))
        with pytest.raises(ValueError, match="receipt identity"):
            ledger.apply_merge(replace(receipt, consumed_positions=(replace(receipt.consumed_positions[0], cost_usdc=Decimal("4")),
                                                                     receipt.consumed_positions[1])))
        assert ledger.snapshot() == state
        next_terms = ReservationTerms(("yes", "no"), Decimal("10"), (Decimal("0.01"), Decimal("0.01")), 1)
        assert ledger.reserve_market("market-1", next_terms, projected_market_unpaired=Decimal("10"),
                                     current_unpaired_cost=Decimal("0"), proposed_unpaired_cost=Decimal("5.60"))
        assert ledger.snapshot().capital.total == Decimal("10")
        ledger.apply_fill(yes_fill)
        assert ledger.snapshot().capital.total == Decimal("10")
        assert ledger.snapshot().realized_profit == Decimal("0.30")


@pytest.mark.parametrize("scenario", ["live", "merged"])
def test_recover_from_sdk_facts(scenario, monkeypatch):
    original, requests = prepared_ledger()
    old = (Position("old-lp", "market-1", "yes", Decimal("7"), Decimal("2.80"), AT),
           Position("old-lp", "market-1", "no", Decimal("7"), Decimal("3.50"), AT))
    fake = FakeSDK(balance=Balance(Decimal("100"), AT), positions=old)
    yes, no = place_pair(fake, requests)
    for acknowledgement in (yes, no):
        original.apply_order(acknowledgement)
    quantities = ("4", "2") if scenario == "live" else ("10", "10")
    for ack, quantity, price, fee, at in (
        (yes, quantities[0], "0.40", "0.04" if scenario == "live" else "0.10", T1),
        (no, quantities[1], "0.55", "0.02" if scenario == "live" else "0.10", T2),
    ):
        fill = fake.execute_fill(ack.order.order_id, trade_id=ack.request.request_id + "-trade",
                                 quantity=Decimal(quantity), price=Decimal(price), fee_usdc=Decimal(fee), at=at)
        original.apply_fill(fill)
    if scenario == "merged":
        merge = MergeRequest("settled-pair", OWNER, "market-1", "yes", "no", Decimal("10"), T3)
        assert original.register_merge(merge)
        fake.script_merge(merge, Outcome.SUCCESS)
        original.apply_merge(fake.merge(merge, at=T3))
        original.apply_merge(fake.receive_merge(merge, at=T3 + timedelta(seconds=1)))
        original.reconcile(fake)
    expected = original.snapshot()
    recovered = Ledger(OWNER, Decimal("5"))
    recovered.reconcile(fake)
    state = recovered.snapshot()
    assert state == replace(expected, startup_cap=Decimal("5"))
    assert tuple(p for p in fake.read_positions() if p.owner == "old-lp") == old
    assert all(f.owner == OWNER for f in state.fills)
    assert {f.trade_id for f in state.fills} == {"yes-trade", "no-trade"}
    recovered.reconcile(fake)
    assert recovered.snapshot() == state
    if scenario == "live":
        assert state.positions == (
            Position(OWNER, "market-1", "no", Decimal("2"), Decimal("1.12"), T2),
            Position(OWNER, "market-1", "yes", Decimal("4"), Decimal("1.64"), T1),
        )
        assert {o.request.quote.token_id: o.remaining_quantity for o in state.orders} == {
            "yes": Decimal("6"), "no": Decimal("8"),
        }
        assert state.cash == Decimal("97.24")
        assert state.capital == CapitalBreakdown(Decimal("2.76"), Decimal("6.80"), Decimal("0"),
                                                Decimal("0.30"), Decimal("0.14"), Decimal("10"))
        assert state.realized_profit == Decimal("0")
        assert recovered.reserve_market("new-market", ReservationTerms(("other-yes", "other-no"), Decimal("1"),
                                         (Decimal("0.01"), Decimal("0.01"))),
                                         projected_market_unpaired=Decimal("1"), current_unpaired_cost=Decimal("1.64"),
                                         proposed_unpaired_cost=Decimal("0.56")) is False
        assert recovered.snapshot() == state
        assert recovery_action_allowed("hedge", covered_hedge=True) is True
        assert recovery_action_allowed("hedge", covered_hedge=False) is False
        assert recovery_action_allowed("merge", covered_hedge=False) is True
        assert recovery_action_allowed("exit", covered_hedge=False) is True
        assert recovery_action_allowed("open_market", covered_hedge=True) is False
    else:
        assert state.cash == Decimal("100.30")
        assert state.capital == CapitalBreakdown(*(Decimal("0"),) * 6)
        assert state.realized_profit == Decimal("0.30")
        missing_proof = replace(fake.read_balance(), merge_results=())
        with monkeypatch.context() as patch:
            patch.setattr(fake, "read_balance", lambda: missing_proof)
            incomplete = Ledger(OWNER, Decimal("5"))
            before = incomplete.snapshot()
            with pytest.raises(ValueError, match="reconcile"):
                incomplete.reconcile(fake)
            assert incomplete.snapshot() == before


@pytest.mark.parametrize("scenario", ["accepted", "rejected", "held_rejection"])
def test_pending_unknown_requires_confirmation(scenario, monkeypatch):
    ledger, requests = prepared_ledger()
    fake = FakeSDK(balance=Balance(Decimal("100"), AT))
    registered = ledger.snapshot()
    assert fake.read_orders() == ()
    ledger.reconcile(fake)
    assert ledger.snapshot() == registered
    unknown = []
    for request in requests:
        fake.script_place(request, Outcome.UNKNOWN)
        result = fake.place_order(request, at=AT)
        assert result.order is None
        unknown.append(result)
        ledger.apply_order(result)
    before = ledger.snapshot()
    assert before.pending == (requests[1], requests[0])
    assert before.orders == ()
    assert before.capital == CapitalBreakdown(Decimal("0"), Decimal("0"), Decimal("9.50"),
                                             Decimal("0.30"), Decimal("0.20"), Decimal("10"))
    # An active-order list omits unresolved submissions and is not a rejection.
    active_orders = tuple(fact for fact in fake.read_orders() if isinstance(fact, Order))
    assert active_orders == ()
    with monkeypatch.context() as patch:
        patch.setattr(fake, "read_orders", lambda: active_orders)
        ledger.reconcile(fake)
    assert ledger.snapshot() == before
    ledger.reconcile(fake)
    assert ledger.snapshot() == before
    recovered_unknown = Ledger(OWNER, Decimal("100"))
    recovered_unknown.reconcile(fake)
    assert recovered_unknown.snapshot() == before
    for request in requests:
        assert ledger.register_order(request) is False
    with pytest.raises(ValueError, match="request identity"):
        ledger.register_order(replace(requests[0], quote=replace(requests[0].quote, quantity=Decimal("11"))))
    assert ledger.snapshot() == before
    # Expiry/another empty lookup proves no rejection and cannot free capacity.
    fake.script_wallet_balance(Decimal("100"), at=AT + timedelta(hours=2))
    ledger.reconcile(fake)
    assert ledger.snapshot() == before
    if scenario == "rejected":
        for request in requests:
            rejection = fake.resolve_place(request, Outcome.FAILED, at=AT + timedelta(hours=2))
            assert rejection == OrderResult(request, Outcome.FAILED, None, AT + timedelta(hours=2))
            ledger.apply_order(rejection)
        state = ledger.snapshot()
        assert state.orders == ()  # A failed request has no invented exchange order/id.
        assert state.pending == ()
        assert len(state.rejected) == 2
        assert state.capital == CapitalBreakdown(*(Decimal("0"),) * 6)
        assert state.reservations[0].planned_quantity == Decimal("0")
        assert state.cash == Decimal("100")
        ledger.reconcile(fake)
        recovered = Ledger(OWNER, Decimal("100"))
        recovered.reconcile(fake)
        assert recovered.snapshot() == ledger.snapshot() == state
        for result in unknown:
            ledger.apply_order(result)
            assert ledger.register_order(result.request) is False
        assert ledger.snapshot() == state
        return
    accepted = fake.resolve_place(requests[0], Outcome.SUCCESS, at=T1)
    assert accepted.order.authoritative is True
    ledger.reconcile(fake)
    state = ledger.snapshot()
    assert state.orders == (accepted.order,)
    assert state.pending == (requests[1],)
    assert state.capital == CapitalBreakdown(Decimal("0"), Decimal("4.00"), Decimal("5.50"),
                                            Decimal("0.30"), Decimal("0.20"), Decimal("10"))
    with pytest.raises(ValueError, match="request identity"):
        ledger.apply_order(replace(accepted, request=replace(requests[0], quote=replace(requests[0].quote, price=Decimal("0.41")))))
    with pytest.raises(ValueError, match="rejection"):
        ledger.apply_order(OrderResult(requests[0], Outcome.FAILED, None, T2))
    assert ledger.snapshot() == state
    if scenario == "held_rejection":
        fill = fake.execute_fill(accepted.order.order_id, trade_id="held-ten", quantity=Decimal("10"),
                                  price=Decimal("0.40"), fee_usdc=Decimal("0.10"), at=T2)
        ledger.apply_fill(fill)
        state = ledger.snapshot()
        held = Position(OWNER, "market-1", "yes", Decimal("10"), Decimal("4.10"), T2)
        assert state.positions == (held,)
        assert state.capital.total == Decimal("10")
        market = Market("market-1", (Token("yes", "Yes"), Token("no", "No")), Decimal("0.01"),
                        Decimal("1"), Decimal("10"), 0.60, 0.61, 2000.0, 10000.0, 100.0, 5.0,
                        0.25, 400, True, True, True, False, True)
        args = dict(market=market, held=held, opposite=Position(OWNER, "market-1", "no", Decimal("0"), Decimal("0"), AT),
                    opposite_book=Book("market-1", "no", Decimal("0.60"), Decimal("0.61"), T3),
                    safety_cushion=Decimal("0.02"), remaining_fee_per_pair=Decimal("0.01"))
        assert hedge_quote(**args, orders=state.orders, pending=state.pending) is None
        rejection = fake.resolve_place(requests[1], Outcome.FAILED, at=T3)
        ledger.reconcile(fake)
        state = ledger.snapshot()
        assert state.rejected == (rejection,)
        assert state.pending == ()
        assert state.capital == CapitalBreakdown(Decimal("4.10"), Decimal("0"), Decimal("0"),
                                                Decimal("5.80"), Decimal("0.10"), Decimal("10"))
        assert hedge_quote(**args, orders=state.orders, pending=state.pending) == Quote("market-1", "no", Decimal("0.56"), Decimal("10"))


@pytest.mark.parametrize("cap", ["10", "100"])
def test_order_registration_stays_within_reserved_capital(cap):
    ledger = Ledger(OWNER, Decimal(cap), balance=Balance(Decimal("100"), AT))
    assert ledger.reserve_market("market-1", TERMS, projected_market_unpaired=Decimal("10"),
                                 current_unpaired_cost=Decimal("0"), proposed_unpaired_cost=Decimal("4.10"))
    yes = OrderRequest("yes", OWNER, Quote("market-1", "yes", Decimal("0.40"), Decimal("10")),
                       AT, AT + timedelta(hours=1), TERMS)
    no = replace(yes, request_id="no", quote=replace(yes.quote, token_id="no"))
    assert ledger.register_order(yes)
    assert ledger.snapshot().capital.total == Decimal("10")
    before = ledger.snapshot()
    expensive = replace(no, quote=replace(no.quote, price=Decimal("0.61")))
    with pytest.raises(ValueError, match="reservation"):
        ledger.register_order(expensive)
    assert ledger.snapshot() == before
    assert ledger.register_order(no)
    assert ledger.snapshot().capital == CapitalBreakdown(Decimal("0"), Decimal("0"), Decimal("8.00"),
                                                        Decimal("1.80"), Decimal("0.20"), Decimal("10"))
    before = ledger.snapshot()
    with pytest.raises(ValueError, match="reservation"):
        ledger.register_order(replace(yes, request_id="extra-yes"))
    assert ledger.snapshot() == before
    assert ledger.register_order(yes) is False
    assert ledger.snapshot() == before

    original, requests = prepared_ledger()
    fake = FakeSDK(balance=Balance(Decimal("100"), AT))
    fake.script_place(requests[0], Outcome.SUCCESS)
    accepted = fake.place_order(requests[0], at=AT)
    original.apply_order(accepted)
    original.apply_fill(fake.execute_fill(accepted.order.order_id, trade_id="covered-ten", quantity=Decimal("10"),
                                          price=Decimal("0.40"), fee_usdc=Decimal("0.10"), at=T1))
    fake.script_place(requests[1], Outcome.FAILED)
    original.apply_order(fake.place_order(requests[1], at=AT))
    recovered = Ledger(OWNER, Decimal("5"))
    recovered.reconcile(fake)
    assert recovered.snapshot().capital.total == Decimal("10")
    hedge = OrderRequest("covered-hedge", OWNER, Quote("market-1", "no", Decimal("0.56"), Decimal("10")),
                         T2, T2 + timedelta(hours=1), recovered.reservation_terms("market-1"))
    assert recovered.register_order(hedge)
    assert recovered.snapshot().capital == CapitalBreakdown(Decimal("4.10"), Decimal("0"), Decimal("5.60"),
                                                            Decimal("0.20"), Decimal("0.10"), Decimal("10"))
    assert recovered.register_order(hedge) is False


@pytest.mark.parametrize("state", ["unsubmitted", "unknown", "acknowledged", "unreconciled_receipt", "failed"])
def test_merge_registration_reserves_inventory_once(state):
    ledger, fake = matched_pair()
    first = MergeRequest("first-six", OWNER, "market-1", "yes", "no", Decimal("6"), T3)
    assert ledger.register_merge(first)
    if state != "unsubmitted":
        outcome = {"unknown": Outcome.UNKNOWN, "failed": Outcome.FAILED}.get(state, Outcome.SUCCESS)
        fake.script_merge(first, outcome)
        ledger.apply_merge(fake.merge(first, at=T3))
        if state == "unreconciled_receipt":
            ledger.apply_merge(fake.receive_merge(first, at=T3 + timedelta(seconds=1)))
    before = ledger.snapshot()
    assert before.capital.total == Decimal("10")
    assert before.realized_profit == Decimal("0")
    assert ledger.register_merge(first) is False
    with pytest.raises(ValueError, match="merge identity"):
        ledger.register_merge(replace(first, quantity=Decimal("5")))
    assert ledger.snapshot() == before
    if state == "failed":
        retry = replace(first, merge_id="definitive-failure-retry", quantity=Decimal("10"))
        assert ledger.register_merge(retry)
        after_retry = ledger.snapshot()
        ledger.apply_merge(replace(before.merges[0], outcome=Outcome.UNKNOWN, observed_at=T3 + timedelta(seconds=10)))
        assert ledger.snapshot() == after_retry
        with pytest.raises(ValueError, match="inventory"):
            ledger.register_merge(replace(first, merge_id="one-too-many", quantity=Decimal("1")))
        assert ledger.snapshot() == after_retry
    else:
        with pytest.raises(ValueError, match="inventory"):
            ledger.register_merge(replace(first, merge_id="overlap-five", quantity=Decimal("5")))
        assert ledger.snapshot() == before
        assert ledger.register_merge(replace(first, merge_id="remaining-four", quantity=Decimal("4")))
    assert ledger.snapshot().capital.total == Decimal("10")
    assert ledger.snapshot().realized_profit == Decimal("0")

@pytest.mark.parametrize("outcome", [Outcome.UNKNOWN, Outcome.SUCCESS, Outcome.FAILED])
def test_recovery_retains_unsettled_merge_identity(outcome, monkeypatch):
    original, fake = matched_pair()
    request = MergeRequest("unsettled-ten", OWNER, "market-1", "yes", "no", Decimal("10"), T3)
    assert original.register_merge(request)
    fake.script_merge(request, outcome)
    result = fake.merge(request, at=T3)
    original.apply_merge(result)
    recovered = Ledger(OWNER, Decimal("100"))
    recovered.reconcile(fake)
    state = recovered.snapshot()
    assert state.merges == (result,)
    assert state == original.snapshot()
    assert state.capital.total == Decimal("10")
    assert state.cash == Decimal("90.30")
    assert state.realized_profit == Decimal("0")
    assert fake.read_balance().merge_results == (result,)
    assert result.received_usdc == Decimal("0") and result.consumed_positions == ()
    assert recovered.register_merge(request) is False
    retry = replace(request, merge_id="new-ten")
    if outcome == Outcome.FAILED:
        assert recovered.register_merge(retry)
    else:
        with pytest.raises(ValueError, match="inventory"):
            recovered.register_merge(retry)
        assert recovered.snapshot() == state
    if outcome == Outcome.UNKNOWN:
        positions, cash = fake.read_positions(), fake.read_balance().usdc
        resolved = fake.resolve_merge(request, Outcome.FAILED, at=T3 + timedelta(seconds=1))
        assert resolved.outcome == Outcome.FAILED
        assert resolved.received_usdc == Decimal("0") and resolved.consumed_positions == ()
        assert fake.read_positions() == positions
        assert fake.read_balance().usdc == cash == Decimal("90.30")
        assert fake.read_balance().merge_results == (resolved,)
        assert fake.resolve_merge(request, Outcome.FAILED, at=T3 + timedelta(seconds=2)) == resolved
        recovered.reconcile(fake)
        assert recovered.snapshot().merges == (resolved,)
        assert recovered.snapshot().capital.total == Decimal("10")
        assert recovered.register_merge(retry)
        before_stale = recovered.snapshot()
        recovered.apply_merge(replace(result, observed_at=T3 + timedelta(seconds=5)))
        assert recovered.snapshot() == before_stale
    without_history = replace(fake.read_balance(), merge_results=None)
    with monkeypatch.context() as patch:
        patch.setattr(fake, "read_balance", lambda: without_history)
        fresh = Ledger(OWNER, Decimal("100"))
        before = fresh.snapshot()
        with pytest.raises(ValueError, match="merge history"):
            fresh.reconcile(fake)
        assert fresh.snapshot() == before



@pytest.mark.parametrize("old_state", ["live", "unknown", "cancelled", "rejected", "settled", "stale_terminal"])
def test_recovery_cannot_hide_older_generation_live_orders(old_state, monkeypatch):
    fake = FakeSDK(balance=Balance(Decimal("100"), AT))
    stale = []
    for token in ("yes", "no"):
        request = OrderRequest("g0-" + token, OWNER, Quote("market-1", token, Decimal("0.40"), Decimal("10")),
                               AT, AT + timedelta(hours=1), TERMS)
        outcome = {"unknown": Outcome.UNKNOWN, "rejected": Outcome.FAILED}.get(old_state, Outcome.SUCCESS)
        fake.script_place(request, outcome)
        acknowledgement = fake.place_order(request, at=AT)
        if old_state in ("cancelled", "stale_terminal"):
            stale.append(acknowledgement.order)
            fake.script_cancel(acknowledgement.order.order_id, Outcome.SUCCESS, cancelled=True)
            fake.cancel_order(acknowledgement.order.order_id, at=T1)
        elif old_state == "settled":
            fake.execute_fill(acknowledgement.order.order_id, trade_id=token + "-old-fill", quantity=Decimal("10"),
                              price=Decimal("0.40"), fee_usdc=Decimal("0.10"), at=T1)
        elif old_state == "rejected":
            stale.append(replace(acknowledgement, outcome=Outcome.UNKNOWN, observed_at=T2))
    if old_state == "settled":
        merge = MergeRequest("old-settled", OWNER, "market-1", "yes", "no", Decimal("10"), T2)
        fake.script_merge(merge, Outcome.SUCCESS)
        fake.merge(merge, at=T2)
        fake.receive_merge(merge, at=T3)
    for token in ("yes", "no"):
        request = OrderRequest("g1-" + token, OWNER, Quote("market-1", token, Decimal("0.40"), Decimal("10")),
                               T3 + timedelta(seconds=1), AT + timedelta(hours=1), replace(TERMS, generation=1))
        fake.script_place(request, Outcome.SUCCESS)
        fake.place_order(request, at=request.created_at)
    facts = fake.read_orders()
    if old_state == "live":
        assert len(facts) == 4
        assert sum(o.remaining_quantity * o.request.quote.price for o in facts) == Decimal("16")
    if old_state in ("stale_terminal", "rejected"):
        monkeypatch.setattr(fake, "read_orders", lambda: facts + tuple(stale))
    recovered = Ledger(OWNER, Decimal("100"))
    before = recovered.snapshot()
    if old_state in ("live", "unknown"):
        with pytest.raises(ValueError, match="older generation"):
            recovered.reconcile(fake)
        assert recovered.snapshot() == before
        return
    recovered.reconcile(fake)
    state = recovered.snapshot()
    assert state.capital == CapitalBreakdown(Decimal("0"), Decimal("8.00"), Decimal("0"),
                                             Decimal("1.80"), Decimal("0.20"), Decimal("10"))
    assert state.cash == (Decimal("101.80") if old_state == "settled" else Decimal("100"))
    assert state.realized_profit == (Decimal("1.80") if old_state == "settled" else Decimal("0"))
    assert len(state.requests) == 4
    assert state.pending == ()
    assert len(state.orders) == (2 if old_state == "rejected" else 4)
    assert state.reservations[0].request_ids == ("g1-no", "g1-yes")
    recovered.reconcile(fake)
    assert recovered.snapshot() == state


@pytest.mark.parametrize("hedge_acknowledged", [True, False], ids=["live_hedge", "pending_hedge"])
def test_confirmed_cancel_releases_spare_capacity_before_hedge_fill(hedge_acknowledged):
    ledger, requests = prepared_ledger()
    fake = FakeSDK(balance=Balance(Decimal("100"), AT))
    yes, no = place_pair(fake, requests)
    for result in (yes, no):
        ledger.apply_order(result)
    ledger.apply_fill(fake.execute_fill(yes.order.order_id, trade_id="yes-partial", quantity=Decimal("4"),
                                        price=Decimal("0.40"), fee_usdc=Decimal("0.04"), at=AT + timedelta(seconds=30)))
    fake.script_cancel(no.order.order_id, Outcome.SUCCESS, cancelled=True)
    ledger.apply_order(fake.cancel_order(no.order.order_id, at=AT + timedelta(seconds=60)))
    hedge = OrderRequest("hedge-four", OWNER, Quote("market-1", "no", Decimal("0.56"), Decimal("4")),
                         AT + timedelta(seconds=61), AT + timedelta(hours=1), ledger.reservation_terms("market-1"))
    assert ledger.register_order(hedge)
    fake.script_place(hedge, Outcome.SUCCESS if hedge_acknowledged else Outcome.UNKNOWN)
    hedge_ack = fake.place_order(hedge, at=hedge.created_at)
    ledger.apply_order(hedge_ack)
    before = ledger.snapshot()
    assert before.capital == CapitalBreakdown(
        Decimal("1.64"), Decimal("4.64") if hedge_acknowledged else Decimal("2.40"),
        Decimal("0") if hedge_acknowledged else Decimal("2.24"), Decimal("3.56"), Decimal("0.16"), Decimal("10"),
    )
    for second, outcome in ((62, Outcome.UNKNOWN), (63, Outcome.FAILED)):
        fake.script_cancel(yes.order.order_id, outcome, cancelled=False)
        ledger.apply_order(fake.cancel_order(yes.order.order_id, at=AT + timedelta(seconds=second)))
        assert ledger.snapshot().capital == before.capital
    fake.script_cancel(yes.order.order_id, Outcome.SUCCESS, cancelled=True)
    cancelled = fake.cancel_order(yes.order.order_id, at=AT + timedelta(seconds=64))
    ledger.apply_order(cancelled)
    after_cancel = ledger.snapshot()
    assert after_cancel.capital == CapitalBreakdown(
        Decimal("1.64"), Decimal("2.24") if hedge_acknowledged else Decimal("0"),
        Decimal("0") if hedge_acknowledged else Decimal("2.24"), Decimal("0.08"), Decimal("0.04"), Decimal("4"),
    )
    assert after_cancel.reservations[0].planned_quantity == Decimal("4")
    assert after_cancel.reservations[0].remaining_fee_usdc == Decimal("0.04")
    for _ in range(2):
        ledger.apply_order(cancelled)
        assert ledger.snapshot() == after_cancel
        ledger.reconcile(fake)
        assert ledger.snapshot() == after_cancel
    recovered = Ledger(OWNER, Decimal("100"))
    recovered.reconcile(fake)
    assert recovered.snapshot() == after_cancel
    recovered.reconcile(fake)
    assert recovered.snapshot() == after_cancel
    if not hedge_acknowledged:
        hedge_ack = fake.resolve_place(hedge, Outcome.SUCCESS, at=AT + timedelta(seconds=65))
        ledger.apply_order(hedge_ack)
        assert ledger.snapshot().capital == CapitalBreakdown(Decimal("1.64"), Decimal("2.24"), Decimal("0"),
                                                            Decimal("0.08"), Decimal("0.04"), Decimal("4"))
    fill = fake.execute_fill(hedge_ack.order.order_id, trade_id="hedge-fill", quantity=Decimal("4"),
                             price=Decimal("0.56"), fee_usdc=Decimal("0.04"), at=AT + timedelta(seconds=66))
    ledger.apply_fill(fill)
    after_fill = ledger.snapshot()
    assert after_fill.capital == CapitalBreakdown(Decimal("3.92"), Decimal("0"), Decimal("0"),
                                                  Decimal("0.08"), Decimal("0"), Decimal("4"))
    assert after_fill.cash == Decimal("96.08")
    ledger.apply_fill(fill)
    ledger.apply_order(cancelled)
    ledger.reconcile(fake)
    assert ledger.snapshot() == after_fill
    recovered.reconcile(fake)
    assert recovered.snapshot() == after_fill
