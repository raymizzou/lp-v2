"""Approved #36 readback and live-control behavior, using external fakes only."""
from datetime import timedelta
from decimal import Decimal as D

import pytest

from tests.test_live_runtime import world, inputs
from tests.test_live_sdk import AT, CID, SIGNER, TOKENS
from tests.test_readonly_sdk import step_at


@pytest.mark.parametrize('field', ['market', 'token', 'wallet', 'side', 'fee'])
def test_owned_fill_uses_signed_intent(tmp_path, field):
    runner, exchange, sdk, journal, observations = world(tmp_path, (inputs(no='.60'),))
    exchange.reject_tokens.add('102')
    step_at(runner, AT)
    exchange.fill('order-1', '10', '.40')
    raw = exchange.trades[0]
    changes = {'token': {'token_id': 'foreign'}, 'wallet': {'maker_address': SIGNER},
               'side': {'side': 'SELL'}, 'fee': {'fee_rate_bps': D('1')}}
    if field == 'market':
        exchange.trades[0] = raw.model_copy(update={'condition_id': 'foreign'})
    else:
        exchange.trades[0] = raw.model_copy(update={
            'maker_orders': (raw.maker_orders[0].model_copy(update=changes[field]),)})
    exchange.reject_tokens.clear()
    summary = step_at(runner, AT + timedelta(seconds=10))
    assert summary['blocker'] == 'UNKNOWN'
    fill, = runner.ledger.snapshot().fills
    assert (fill.condition_id, fill.token_id, fill.quantity, fill.price, fill.fee_usdc) == (
        CID, TOKENS[0], D('10'), D('.40'), D('0'))
    assert len(exchange.posts) == 3
    hedge = runner.ledger.snapshot().requests[-1]
    assert hedge.purpose == 'hedge' and hedge.quote.quantity == D('10')
    assert hedge.quote.price == D('.57')
    sdk.close()


@pytest.mark.parametrize('residual', [D('0'), D('0.008133')])
def test_external_residual_does_not_block_exit(tmp_path, residual):
    from tests.test_rotation_live import rotation_world, BID
    rotation, exchange, observations, clock = rotation_world(tmp_path)
    old = rotation.current
    inventory_reads = []
    read_inventory = observations.read_conditional_inventory
    def inventory(now, cid):
        inventory_reads.append(cid)
        return read_inventory(now, cid)
    observations.read_conditional_inventory = inventory
    exchange.quantities['101'] = residual
    try:
        first = rotation.step(AT)
        assert first['blocker'] == 'UNKNOWN' and len(exchange.posts) == 2
        clock[0] = AT + timedelta(seconds=10)
        rotation.scanner.scan_once(clock[0])
        report = rotation.step(clock[0])
        assert report['rotation']['phase'] == 'running'
        assert report['rotation']['current'] == BID
        assert report['blocker'] == 'UNKNOWN'
        assert old.ledger.snapshot().positions == ()
        assert inventory_reads == []
        assert all(c.kwargs['asset_type'] != 'CONDITIONAL'
                   for c in exchange.sdk.get_balance_allowance.call_args_list)
        assert len(exchange.posts) == 4
        assert [s.token_id for s in exchange.posts] == ['101', '102', '201', '202']
        assert [c.kwargs['order_id'] for c in exchange.sdk.cancel_order.call_args_list] == ['order-1', 'order-2']
        assert old.sdk.journal.events('merge_intent') == ()
        assert rotation.sdk.journal.events('merge_intent') == ()
        assert exchange.quantities['101'] == residual
    finally:
        rotation.close()


@pytest.mark.parametrize('status', ['MINED', 'FAILED'])
def test_unconfirmed_fill_waits_for_confirmation(tmp_path, status):
    runner, exchange, sdk, journal, observations = world(tmp_path, (inputs(no='.60'),))
    exchange.reject_tokens.add('102')
    step_at(runner, AT)
    exchange.fill('order-1', '10', '.40')
    confirmed = exchange.trades[0]
    exchange.trades[0] = confirmed.model_copy(update={'status': status})
    exchange.reject_tokens.clear()
    for seconds in (10, 20):
        summary = step_at(runner, AT + timedelta(seconds=seconds))
        assert summary['blocker'] == 'UNKNOWN'
        assert runner.ledger.snapshot().fills == ()
        assert len(exchange.posts) == 2
    assert len([p for _, p in journal.events('diagnostic') if p.get('reason') ==
                ('fill_failed' if status == 'FAILED' else 'fill_unconfirmed')]) == 1
    if status == 'MINED':
        exchange.trades[0] = confirmed
        for seconds in (30, 40):
            summary = step_at(runner, AT + timedelta(seconds=seconds))
            assert summary['blocker'] == 'UNKNOWN'
            fill, = runner.ledger.snapshot().fills
            assert (fill.quantity, fill.price, fill.fee_usdc) == (D('10'), D('.40'), D('0'))
        assert len(journal.events('fill')) == 1 and len(exchange.posts) == 3
    sdk.close()


@pytest.mark.parametrize('quantity', [D('0.008133'), D('10')])
def test_owned_inventory_does_not_delay_exit(tmp_path, quantity):
    from tests.test_rotation_live import rotation_world, BID
    rotation, exchange, observations, clock = rotation_world(tmp_path)
    old = rotation.current
    exchange.reject_tokens.add('102')
    try:
        rotation.step(AT)
        exchange.fill('order-1', str(quantity), '.40', at=AT + timedelta(seconds=1))
        exchange.reject_tokens.clear()
        clock[0] = AT + timedelta(seconds=10)
        rotation.scanner.scan_once(clock[0])
        report = rotation.step(clock[0])
        assert report['rotation']['phase'] == 'running' and report['rotation']['current'] == BID
        position, = old.ledger.snapshot().positions
        assert (position.quantity, position.cost_usdc) == (quantity, quantity * D('.40'))
        assert exchange.quantities['101'] == quantity
        assert [s.token_id for s in exchange.posts] == ['101', '102', '201', '202']
        assert all(p['request'].purpose == 'opening' for _, p in old.sdk.journal.events('intent'))
        assert old.sdk.journal.events('merge_intent') == ()
        assert all(c.kwargs['asset_type'] != 'CONDITIONAL'
                   for c in exchange.sdk.get_balance_allowance.call_args_list)
    finally:
        rotation.close()


def test_exit_books_late_owned_fill_without_inventory_wait(tmp_path):
    from tests.test_rotation_live import rotation_world, BID
    rotation, exchange, observations, clock = rotation_world(tmp_path)
    old = rotation.current
    cancel = exchange.cancel
    def late_cancel(*, order_id):
        result = cancel(order_id=order_id)
        if order_id == 'order-1':
            exchange.fill(order_id, '4', '.40', at=clock[0])
        return result
    try:
        rotation.step(AT)
        exchange.sdk.cancel_order.side_effect = late_cancel
        clock[0] += timedelta(seconds=10)
        rotation.scanner.scan_once(clock[0])
        report = rotation.step(clock[0])
        assert report['rotation']['phase'] == 'running' and report['rotation']['current'] == BID
        fill, = old.ledger.snapshot().fills
        assert (fill.token_id, fill.quantity, fill.price) == ('101', D('4'), D('.40'))
        assert old.ledger.snapshot().positions[0].cost_usdc == D('1.60')
        assert old.sdk.journal.events('merge_intent') == ()
        assert all(p['request'].purpose == 'opening' for _, p in old.sdk.journal.events('intent'))
        assert len(exchange.posts) == 4
    finally:
        rotation.close()


def test_drain_missing_history_accepts_complete_empty_list(tmp_path):
    runner, exchange, sdk, journal, observations = world(tmp_path)
    exchange.sdk.post_order.side_effect = TimeoutError('synthetic-secret-never-log')
    try:
        step_at(runner, AT)
        pending = runner.ledger.snapshot().pending
        assert len(pending) == 2 and exchange.orders == {}
        assert len(sdk.owned_candidate_ids()) == 4
        assert runner.ledger.snapshot().capital.total == D('10')
        assert runner.drain(timeout_s=0) == 0
        stopped = journal.events('stop_result')[-1][1]
        assert stopped['exit_code'] == 0 and stopped['reconciliation'] == 'UNKNOWN'
        assert stopped['pending'] == pending and stopped['orders'] == ()
        assert runner.ledger.snapshot().capital.total == D('10')
        assert exchange.sdk.post_order.call_count == 2
        assert all(result.outcome.value == 'unknown' for _, result in journal.events('order_result'))
        assert not exchange.sdk.cancel_order.called
        assert 'synthetic-secret-never-log' not in str(journal.events())
    finally:
        sdk.close()


@pytest.mark.parametrize('proof', ['signed-present', 'partial-page'])
def test_drain_requires_complete_list_without_owned_candidates(tmp_path, proof):
    from types import SimpleNamespace
    runner, exchange, sdk, journal, observations = world(tmp_path)
    submitted = []
    def unknown(signed):
        submitted.append(signed)
        raise TimeoutError('synthetic-secret-never-log')
    exchange.sdk.post_order.side_effect = unknown
    try:
        step_at(runner, AT)
        assert len(runner.ledger.snapshot().pending) == 2
        if proof == 'signed-present':
            exchange.post(submitted[0])
            raw = exchange.orders.pop('order-1')
            candidate = journal.events('signed')[0][1]['order_ids'][0]
            exchange.orders[candidate] = raw.model_copy(update={'id': candidate})
            exchange.orders['foreign'] = raw.model_copy(update={'id': 'foreign'})
            exchange.cancel_unknown = True
            exchange.sdk.get_order.side_effect = lambda **kw: None
            assert sdk.known_order_ids() == () and candidate in sdk.owned_candidate_ids()
        else:
            def incomplete(**kw):
                yield SimpleNamespace(items=())
                raise TimeoutError('synthetic-secret-never-log')
            exchange.sdk.list_open_orders.side_effect = incomplete
        assert runner.drain(timeout_s=0) == 1
        stopped = journal.events('stop_result')[-1][1]
        assert stopped['exit_code'] == 1
        assert runner.ledger.snapshot().capital.total == D('10')
        assert exchange.sdk.post_order.call_count == 2
        assert all(c.kwargs['order_id'] != 'foreign' for c in exchange.sdk.cancel_order.call_args_list)
        if proof == 'signed-present':
            assert exchange.orders[candidate].status == 'LIVE'
            assert exchange.orders['foreign'].status == 'LIVE'
        assert 'synthetic-secret-never-log' not in str(journal.events())
    finally:
        sdk.close()


def test_missing_history_allows_round_without_replay(tmp_path):
    runner, exchange, sdk, journal, observations = world(tmp_path)
    exchange.sdk.post_order.side_effect = TimeoutError('synthetic-secret-never-log')
    try:
        step_at(runner, AT)
        pending = runner.ledger.snapshot().pending
        summary = step_at(runner, AT + timedelta(seconds=10))
        assert summary['blocker'] == 'UNKNOWN'
        assert summary['run']['region_check'] == 'ok'
        assert summary['markets'][0]['eligible'] is True
        assert summary['accounting']['status'] == 'UNKNOWN'
        assert runner.ledger.snapshot().pending == pending and len(pending) == 2
        assert runner.ledger.snapshot().capital.total == D('10')
        assert exchange.sdk.post_order.call_count == 2
        assert exchange.orders == {} and runner.drain(timeout_s=0) == 0
        assert journal.events('stop_result')[-1][1]['reconciliation'] == 'UNKNOWN'
    finally:
        sdk.close()


@pytest.mark.parametrize('window', ['absent', 'expired'])
def test_first_signature_and_rotation_ignore_old_window(tmp_path, monkeypatch, window):
    from pathlib import Path
    from tests.test_rotation_live import factory_boundary, confirm_factory_seed, BID
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.live_control.profiles import atomic_json
    config, exchange, clock, record, seen = factory_boundary(tmp_path, monkeypatch)
    if window == 'absent':
        record['window_end'] = None
        atomic_json(Path(config.state_dir) / 'live-control-run.json', record)
    clock[0] = AT + timedelta(minutes=6)
    rotation = create_live_runner(config, clock=lambda: clock[0])
    try:
        rotation.step(clock[0])
        assert len(exchange.posts) == 2
        assert rotation.action_id == record['action_id']
        confirm_factory_seed(rotation, record, clock, monkeypatch)
        clock[0] += timedelta(seconds=10)
        rotation.scanner.scan_once(clock[0])
        report = rotation.step(clock[0])
        assert report['rotation']['phase'] == 'running' and report['rotation']['current'] == BID
        assert len(exchange.posts) == 4
        assert all(s.token_id in ('201', '202') for s in exchange.posts[2:])
        rotation.close()
        from dataclasses import replace
        rotation = create_live_runner(replace(config, started_at=clock[0]), clock=lambda: clock[0])
        rotation.step(clock[0])
        assert rotation.current.condition_id == BID and len(exchange.posts) == 4
        assert rotation.drain(timeout_s=0) == 0
        assert rotation.stop.is_set()
    finally:
        rotation.close()


@pytest.mark.parametrize('source', ['open-orders', 'trades'])
def test_network_failure_still_cancels_owned(tmp_path, source):
    runner, exchange, sdk, journal, observations = world(tmp_path)
    try:
        step_at(runner, AT)
        exchange.orders['foreign'] = exchange.orders['order-1'].model_copy(update={'id': 'foreign'})
        if source == 'open-orders':
            exchange.sdk.get_order.side_effect = lambda **kwargs: None
            exchange.sdk.list_open_orders.side_effect = TimeoutError('private-failure-sentinel')
        else:
            exchange.sdk.list_account_trades.side_effect = TimeoutError('private-failure-sentinel')
        summary = step_at(runner, AT + timedelta(seconds=10))
        assert summary['blocker'] != 'UNKNOWN'
        assert summary['accounting']['status'] == 'UNKNOWN'
        assert all(row['eligible'] is False for row in summary['markets'])
        assert len(exchange.posts) == 2 and len(journal.events('signed')) == 2
        assert runner.ledger.snapshot().capital.total == D('10')
        assert [c.kwargs['order_id'] for c in exchange.sdk.cancel_order.call_args_list] == ['order-1', 'order-2']
        assert exchange.orders['foreign'].status == 'LIVE'
        assert 'private-failure-sentinel' not in str(summary) + str(journal.events())
    finally:
        sdk.close()


@pytest.mark.parametrize('bad', ['quantity', 'price', 'identity', 'overflow'])
def test_bad_fill_rows_are_skipped_once(tmp_path, bad):
    runner, exchange, sdk, journal, observations = world(tmp_path)
    try:
        step_at(runner, AT)
        exchange.fill('order-1', '4', '.40')
        first, = sdk.read_trades()
        raw = exchange.trades[0]
        maker = raw.maker_orders[0].model_copy(update={
            'matched_amount': D('11' if bad == 'quantity' else '7' if bad == 'overflow' else '3'),
            'price': D('.41' if bad == 'price' else '.40')})
        changed = raw.model_copy(update={'id': raw.id if bad == 'identity' else 'bad-row', 'maker_orders': (maker,)})
        exchange.trades = [changed] if bad == 'identity' else [raw, changed]
        for _ in range(2):
            assert sdk.read_trades() == (first,)
        skips = [p for _, p in journal.events('diagnostic') if p.get('readback_skip')]
        assert len(skips) == 1
        assert len(journal.events('fill')) == 1 and first.quantity == D('4') and first.price == D('.40')
        assert len(exchange.posts) == 2
        from tests.test_live_sdk import adapter
        journal.close()
        restarted, reopened = adapter(tmp_path, exchange.sdk)
        try:
            assert restarted.read_trades() == (first,)
            assert len([p for _, p in reopened.events('diagnostic') if p.get('readback_skip')]) == 1
        finally:
            restarted.close()
    finally:
        sdk.close()


def test_late_fill_books_without_requiring_terminal_object(tmp_path):
    runner, exchange, sdk, journal, observations = world(tmp_path)
    try:
        step_at(runner, AT)
        exchange.terminal('order-1')
        step_at(runner, AT + timedelta(seconds=10))
        terminal = next(o for o in runner.ledger.snapshot().orders if o.order_id == 'order-1')
        assert terminal.authoritative and terminal.status.value == 'cancelled' and terminal.filled_quantity == 0
        exchange.fill('order-1', '2', '.40', at=AT + timedelta(seconds=11))
        get = exchange.sdk.get_order.side_effect
        exchange.sdk.get_order.side_effect = lambda *, order_id: None if order_id == 'order-1' else get(order_id=order_id)
        before = len(exchange.posts)
        for seconds in (20, 30):
            summary = step_at(runner, AT + timedelta(seconds=seconds))
            assert summary['blocker'] == 'UNKNOWN'
            fill, = runner.ledger.snapshot().fills
            assert fill.order_id == 'order-1' and fill.quantity == D('2') and fill.price == D('.40')
            assert runner.ledger.snapshot().positions[0].cost_usdc == D('.80')
        recorded = [row.order for _, row in journal.events('order_result') if row.order and row.order.order_id == 'order-1']
        assert recorded[-1].filled_quantity == 0 and recorded[-1].status.value == 'cancelled'
        assert len(journal.events('fill')) == 1 and len(exchange.posts) == before
    finally:
        sdk.close()


@pytest.mark.parametrize('bad', ['request', 'order', 'fill', 'position', 'generation'])
def test_ledger_readback_keeps_valid_facts_and_skips_conflicts(bad):
    from dataclasses import replace
    from tests.test_ledger import prepared_ledger, place_pair, OWNER, TERMS, T1
    from tests.fakes.fake_sdk import FakeSDK
    from lp_v2.ledger import Ledger
    from lp_v2.models import Balance, Outcome
    ledger, requests = prepared_ledger()
    fake = FakeSDK(balance=Balance(D('100'), AT))
    yes, no = place_pair(fake, requests)
    fill = fake.execute_fill(yes.order.order_id, trade_id='valid-four', quantity=D('4'),
        price=D('.40'), fee_usdc=D('.04'), at=T1)
    ledger.reconcile(fake)
    before = ledger.snapshot()
    if bad in ('request', 'order'):
        orders = fake.read_orders()
        changed = (replace(orders[0], request=replace(requests[0], quote=replace(requests[0].quote, price=D('.41'))))
                   if bad == 'request' else replace(orders[0], filled_quantity=D('11'), remaining_quantity=D('0')))
        fake.read_orders = lambda: (*orders, changed)
    elif bad == 'fill':
        fake.read_trades = lambda: (fill, replace(fill, quantity=D('3')))
    elif bad == 'position':
        positions = fake.read_positions()
        fake.read_positions = lambda: (replace(positions[0], quantity=D('9')),)
    else:
        for request in requests:
            newer = replace(request, request_id='new-' + request.request_id,
                            reservation_terms=replace(TERMS, generation=1))
            fake.script_place(newer, Outcome.SUCCESS)
            fake.place_order(newer, at=AT)
    for _ in range(2):
        ledger.reconcile(fake)
        state = ledger.snapshot()
        assert state.fills == (fill,)
        assert state.positions[0].quantity == D('4') and state.positions[0].cost_usdc == D('1.64')
        assert state.cash == D('98.36')
        if bad == 'generation':
            assert len(state.requests) == len(state.orders) == 4
            assert state.capital.total <= D('100')
        else:
            assert state == before
    assert len(fake.readback_skips) == (1 if bad in ('request', 'order', 'fill') else 0)
    with pytest.raises(ValueError, match='identity'):
        ledger.register_order(replace(requests[0], quote=replace(requests[0].quote, price=D('.41'))))


def test_order_readback_mismatch_keeps_original_reservation(tmp_path):
    runner, exchange, sdk, journal, observations = world(tmp_path)
    try:
        step_at(runner, AT)
        original = runner.ledger.snapshot().requests
        exchange.orders['order-1'] = exchange.orders['order-1'].model_copy(update={'token_id': '999'})
        for seconds in (10, 20):
            report = step_at(runner, AT + timedelta(seconds=seconds))
            assert report['blocker'] == 'UNKNOWN'
            assert runner.ledger.snapshot().requests == original
            assert runner.ledger.snapshot().capital.total == D('10')
            assert not sdk.orders_verified() and len(exchange.posts) == 2
        assert len([p for _, p in journal.events('diagnostic')
                    if p.get('reason') == 'order_readback_invalid']) == 1
        assert {o.request.quote.token_id for o in runner.ledger.snapshot().orders} == {'101', '102'}
    finally:
        sdk.close()


def test_next_entry_does_not_recheck_retired_market(tmp_path):
    from tests.test_rotation_live import rotation_world, BID, CID_C
    rotation, exchange, observations, clock = rotation_world(tmp_path)
    try:
        rotation.step(AT)
        clock[0] += timedelta(seconds=10)
        rotation.scanner.scan_once(clock[0]); rotation.step(clock[0])
        assert rotation.current.condition_id == BID and len(exchange.posts) == 4
        trade_history = exchange.sdk.list_account_trades.side_effect
        retired_reads = []
        def trades(**kwargs):
            if kwargs.get('market') == CID:
                retired_reads.append(CID)
                raise TimeoutError('retired market unavailable')
            return trade_history(**kwargs)
        exchange.sdk.list_account_trades.side_effect = trades
        observations.ranked = (BID, CID_C)
        clock[0] += timedelta(seconds=10)
        rotation.scanner.scan_once(clock[0]); report = rotation.step(clock[0])
        assert report['rotation']['current'] == CID_C and report['rotation']['phase'] == 'running'
        assert retired_reads == []
        assert [p.token_id for p in exchange.posts] == ['101','102','201','202','301','302']
    finally:
        rotation.close()


def test_ineligible_restart_exits_then_rotates_without_new_on(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.test_prb_inventory_exits import active_recovery_world
    from tests.test_rotation_live import BID
    from lp_v2.live_runtime import create_live_runner
    exchange, clock, record, seen, config, state = active_recovery_world(tmp_path, monkeypatch, 'running', 'closed', filled=True)
    exchange.orders['foreign'] = exchange.orders['foreign'].model_copy(update={'status': 'LIVE'})
    rotation = create_live_runner(replace(config, started_at=clock[0]), clock=lambda: clock[0])
    try:
        for _ in range(4):
            clock[0] += timedelta(seconds=10)
            rotation.scanner.scan_once(clock[0]); rotation.step(clock[0])
        assert rotation.current.condition_id == CID and rotation.phase == 'running'
        assert rotation.action_id == record['action_id'] and len(exchange.posts) == 6
        assert all(p.token_id in TOKENS for p in exchange.posts[4:])
        assert exchange.orders['foreign'].status == 'LIVE'
        assert all(c.kwargs['order_id'] != 'foreign' for c in exchange.sdk.cancel_order.call_args_list)
        assert exchange.quantities['201'] == D('1')
        rotation.close()
        rotation = create_live_runner(replace(config, started_at=clock[0]), clock=lambda: clock[0])
        rotation.step(clock[0])
        assert rotation.current.condition_id == CID and len(exchange.posts) == 6
    finally:
        rotation.close()
