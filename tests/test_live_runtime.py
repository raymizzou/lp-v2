from tests.test_readonly_sdk import clocked_sdk, step_at
from dataclasses import replace, FrozenInstanceError
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
import pytest
from tests.test_live_sdk import AT, CID, TOKENS, OWNER, adapter
from tests.fakes.live_exchange import Exchange
from tests.test_scenarios import plan
from lp_v2.models import Book, Token
from lp_v2.cli import RunConfig


def inputs(**kwargs):
    p = plan(**kwargs)
    return replace(p, market=replace(p.market, condition_id=CID, tokens=(Token('101','Yes'),Token('102','No'))),
                   books=tuple(replace(b,condition_id=CID,token_id=t) for b,t in zip(p.books,TOKENS)))


class Observations:
    def __init__(self, plans):
        self.plans = plans
        self.region = {'blocked':False,'country':'US'}
    def read_markets(self, now):
        return [dict(condition_id=p.market.condition_id,title='market',eligible=True,plan=p,score='1',observation_completed_at=now) for p in self.plans]
    def read_region_policy(self):
        return self.region


def world(tmp_path, plans=None):
    from lp_v2.live_runtime import LiveRunner
    ex = Exchange()
    live, journal = adapter(tmp_path, ex.sdk)
    obs = Observations(plans or (inputs(),))
    runner = LiveRunner(RunConfig(D('100'), AT), live, obs, clock=lambda: ex.now)
    return runner, ex, live, journal, obs


def test_single_market_and_cap(tmp_path):
    runner, ex, live, journal, obs = world(tmp_path, (inputs(quantity='100', no='.40'), plan('other',quantity='100')))
    step_at(runner, AT)
    assert len(ex.posts) == 2
    assert {s.token_id for s in ex.posts} == set(TOKENS)
    assert runner.ledger.snapshot().capital.total == D('100')
    assert all(p['total'] <= D('100') and p['condition_id'] == CID for _,p in journal.events('occupation'))
    with pytest.raises((FrozenInstanceError, AttributeError)):
        runner.config.capital_usdc = D('200')
    with pytest.raises(AttributeError):
        runner.condition_id = 'other'
    assert runner.snapshot()['capital_usdc'] == '100'


@pytest.mark.parametrize('region,closed,allowed,health', [
    ({'blocked':False,'country':'US'},False,True,'ok'),
    ({'blocked':True,'country':'JP'},False,True,'blocked'),
    ({'blocked':True,'country':'JP'},True,False,'blocked'),
    ({'blocked':True,'country':'GB'},False,False,'blocked'),
    ({},None,False,'UNKNOWN'),
    ({'blocked':0,'country':'US'},False,False,'UNKNOWN'),
    ({'blocked':True,'country':'JP'},0,False,'blocked'),
], ids=['ok','jp-open','jp-closed','other-blocked','unknown','invalid-blocked','invalid-closed'])
def test_region_gate(tmp_path, region, closed, allowed, health):
    runner, ex, live, journal, obs = world(tmp_path)
    obs.region = region
    ex.sdk.get_closed_only_mode.return_value = closed
    summary = step_at(runner, AT)
    assert bool(ex.posts) is allowed
    assert summary['run']['region_check'] == health
    if not allowed:
        assert journal.events('region_denied')
        assert runner.ledger.snapshot().pending == ()


def test_reconcile_cancel_place_ordering(tmp_path):
    runner, ex, live, journal, obs = world(tmp_path, (inputs(no='.40'),))
    step_at(runner, AT)
    original = tuple(ex.orders)
    ex.cancel_unknown = True
    p = inputs(yes='.46',no='.46')
    obs.plans = (replace(p,market=replace(p.market,rewards_max_spread=2.0),
                         books=tuple(replace(b,observed_at=AT+timedelta(seconds=60)) for b in p.books)),)
    step_at(runner, AT+timedelta(seconds=60))
    assert ex.sdk.list_account_trades.call_count >= 2
    assert set(c.kwargs['order_id'] for c in ex.sdk.cancel_order.call_args_list) == set(original)
    assert len(ex.posts) == 2
    step_at(runner, AT+timedelta(seconds=70))
    assert len(ex.posts) == 2
    for oid in original:
        ex.terminal(oid)
    # Post boundary refuses replacements unless terminal readback was obtained.
    original_post = ex.sdk.post_order.side_effect
    def post(signed):
        assert all(any(call.kwargs['order_id'] == oid for call in ex.sdk.get_order.call_args_list) for oid in original)
        assert all(ex.orders[oid].status == 'CANCELED' for oid in original)
        return original_post(signed)
    ex.sdk.post_order.side_effect = post
    step_at(runner, AT+timedelta(seconds=80))
    assert len(ex.posts) == 4
    step_at(runner, AT+timedelta(seconds=90))
    assert len(ex.posts) == 4


@pytest.mark.parametrize('scenario', ['one-side-full','both-partial','late-fill','unknown-cancel','pending-hedge','expired-holding'])
def test_live_scenarios(tmp_path, scenario):
    runner, ex, live, journal, obs = world(tmp_path)
    if scenario in ('one-side-full','pending-hedge','expired-holding'):
        ex.reject_tokens.add('102')
    step_at(runner, AT)
    if scenario in ('one-side-full','pending-hedge','expired-holding'):
        ex.fill('order-1','10','.40')
        ex.reject_tokens.clear()
        p = inputs(no='.60',reward=0 if scenario == 'expired-holding' else 100)
        when = AT+timedelta(hours=25) if scenario == 'expired-holding' else AT+timedelta(seconds=10)
        obs.plans = (replace(p,books=tuple(replace(b,observed_at=when) for b in p.books)),)
        if scenario == 'pending-hedge':
            ex.sdk.post_order.side_effect = TimeoutError('unknown submission')
        step_at(runner, when)
        state = runner.ledger.snapshot()
        hedges = [r for r in state.requests if r.purpose == 'hedge']
        assert len(hedges) == 1
        assert hedges[0].quote.price == D('.57')
        assert hedges[0].quote.quantity == D('10')
        assert state.capital.inventory_cost == D('4.00')
        step_at(runner, when+timedelta(seconds=10))
        assert len([r for r in runner.ledger.snapshot().requests if r.purpose == 'hedge']) == 1
    else:
        ex.fill('order-1','6','.40')
        if scenario == 'both-partial':
            ex.fill('order-2','2','.55')
        if scenario in ('late-fill','unknown-cancel'):
            ex.cancel_unknown = True
            obs.plans = (inputs(yes='.39',no='.59'),)
            step_at(runner, AT+timedelta(seconds=60))
            if scenario == 'late-fill':
                ex.fill('order-2','2','.55',at=AT+timedelta(seconds=61))
                ex.terminal('order-1')
                ex.terminal('order-2')
        obs.plans = tuple(replace(p,books=tuple(replace(b,observed_at=AT+timedelta(seconds=70)) for b in p.books)) for p in obs.plans)
        step_at(runner, AT+timedelta(seconds=70))
        state = runner.ledger.snapshot()
        assert sum(p.quantity for p in state.positions if p.token_id == '101') == D('6')
        assert state.capital.total >= D('6')
        assert len({f.trade_id for f in state.fills}) == len(state.fills)
        assert all(r.quote.quantity <= D('6') for r in state.requests if r.purpose == 'hedge')
        if scenario == 'unknown-cancel':
            assert len(ex.posts) == 2


def test_live_random_capital(tmp_path):
    from random import Random
    for seed in range(20261102,20261107):
        path = tmp_path/str(seed)
        path.mkdir()
        runner,ex,live,journal,obs = world(path)
        rng = Random(seed)
        history = []
        step_at(runner, AT)
        for tick in range(1,31):
            ex.now = AT+timedelta(seconds=10*tick)
            active = [o for o in ex.orders.values() if o.status == 'LIVE']
            if active:
                order = rng.choice(active)
                if rng.choice((True,False)):
                    amount = min(D(rng.randint(1,3)),order.original_size-order.size_matched)
                    ex.fill(order.id,amount,order.price,at=ex.now-timedelta(seconds=1))
                    history.append(('fill',order.id,str(amount)))
                else:
                    ex.cancel_unknown = rng.choice((True,False))
                    if not ex.cancel_unknown:
                        ex.terminal(order.id)
                    history.append(('cancel',order.id,ex.cancel_unknown))
            obs.plans = tuple(replace(p,books=tuple(replace(b,observed_at=ex.now) for b in p.books)) for p in obs.plans)
            step_at(runner, ex.now)
            state = runner.ledger.snapshot()
            assert 0 <= state.capital.total <= D('100'), (seed,history)
            assert len({r.request_id for r in state.requests}) == len(state.requests), (seed,history)
            quantities = dict(ex.quantities)
            for token in TOKENS:
                live_amount = sum((o.original_size-o.size_matched for o in ex.orders.values() if o.token_id == token and o.status == 'LIVE'),D('0'))
                pending = sum((r.quote.quantity for r in state.pending if r.quote.token_id == token),D('0'))
                assert quantities.get(token,D('0'))+live_amount+pending <= D('10'), (seed,history)
            yes,no = (quantities.get(t,D('0')) for t in TOKENS)
            # Independent M2 inventory-risk oracle: actual unmatched BUY cost.
            unmatched = abs(yes-no)
            heavy = '101' if yes > no else '102'
            cost = sum((m.price*m.matched_amount for t in ex.trades for m in t.maker_orders if m.token_id == heavy),D('0'))
            assert (cost/max(yes,no)*unmatched if max(yes,no) else D('0')) <= D('50'), (seed,history)
            assert state.capital.total >= D('100')-ex.cash, (seed,history)
            for r in state.requests:
                if r.purpose == 'hedge':
                    assert r.quote.price <= D('.57'), (seed,history)
        assert len(ex.posts) == len(journal.events('signed'))


@pytest.mark.parametrize('unknown', [False,True])
def test_stop_requires_empty_owned_readback(tmp_path, unknown):
    runner,ex,live,journal,obs = world(tmp_path)
    step_at(runner, AT)
    ex.fill('order-1','2','.40')
    ex.orders['foreign'] = ex.orders['order-2'].model_copy(update={'id':'foreign'})
    ex.cancel_unknown = unknown
    tick = [0]
    def wait(seconds):
        tick[0] += seconds
        if unknown:
            for oid in ('order-1','order-2'):
                ex.terminal(oid)
    result = runner.drain(timeout_s=5,monotonic=lambda:tick[0],wait=wait)
    assert result == 0
    assert runner.stop.is_set()
    assert {c.kwargs['order_id'] for c in ex.sdk.cancel_order.call_args_list} == {'order-1','order-2'}
    assert ex.orders['foreign'].status == 'LIVE'
    assert runner.ledger.snapshot().positions[0].quantity == D('2')
    assert journal.events('stop_result')[-1][1]['exit_code'] == 0
    assert len(ex.posts) == 2


@pytest.mark.parametrize('mode', ['cancel','placement','read-error'])
def test_stop_timeout_records_residuals(tmp_path, mode):
    runner,ex,live,journal,obs = world(tmp_path)
    if mode == 'placement':
        ex.sdk.post_order.side_effect = TimeoutError('unknown')
    step_at(runner, AT)
    ex.cancel_unknown = True
    if mode == 'read-error':
        ex.sdk.list_open_orders.side_effect = TimeoutError('read unavailable')
    tick = [0]
    def wait(seconds):
        tick[0] += seconds
        assert tick[0] <= 5
    assert runner.drain(timeout_s=5,monotonic=lambda:tick[0],wait=wait) != 0
    residual = journal.events('stop_result')[-1][1]
    assert residual['exit_code'] != 0
    if mode == 'placement':
        assert len(residual['pending']) == 2
    else:
        assert {o.order_id for o in residual['orders']} == {'order-1','order-2'}
    assert tick[0] == 5


def test_restart_preserves_hedge(tmp_path):
    from lp_v2.live_runtime import LiveRunner
    runner,ex,live,journal,obs = world(tmp_path)
    ex.reject_tokens.add('102')
    step_at(runner, AT)
    ex.fill('order-1','10','.40')
    runner.ledger.reconcile(live)
    before = runner.ledger.snapshot().positions
    journal.close()
    restarted_sdk,reopened = adapter(tmp_path,ex.sdk)
    ex.reject_tokens.clear()
    obs.plans = (inputs(no='.60'),)
    restarted = LiveRunner(RunConfig(D('100'),AT+timedelta(seconds=10)),restarted_sdk,obs,clock=lambda:ex.now)
    step_at(restarted, AT+timedelta(seconds=10))
    assert restarted.ledger.snapshot().positions == before
    hedges = [r for r in restarted.ledger.snapshot().requests if r.purpose == 'hedge']
    assert len(hedges) == 1 and hedges[0].quote.price == D('.57') and hedges[0].quote.quantity == D('10')
    count = len(ex.posts)
    step_at(restarted, AT+timedelta(seconds=20))
    assert len(ex.posts) == count
    assert len(reopened.events('intent')) == 3


def test_round_audit_contains_breakdown_and_uncertainty(tmp_path):
    from dataclasses import asdict
    runner,ex,live,journal,obs=world(tmp_path)
    step_at(runner, AT)
    ex.fill('order-1','2','.40',at=AT+timedelta(seconds=1))
    step_at(runner, AT+timedelta(seconds=10))
    audit=journal.events('occupation')[-1][1]
    for key,value in asdict(runner.ledger.snapshot().capital).items():
        assert audit[key]==value
    assert audit['reconciliation']=='SUCCESS'
    assert audit['accounting_as_of']==AT+timedelta(seconds=10)
    held=next(p for p in audit['inventory'] if p['token_id']=='101')
    assert held['unpaired_quantity']==D('2') and held['unpaired_cost_usdc']==D('.80')
    assert held['mark_usdc']==D('.82') and held['mark_at']==AT
    assert audit['uncertainty']['pending']==()
    ex.sdk.list_account_trades.side_effect=TimeoutError('unavailable')
    step_at(runner, AT+timedelta(seconds=80))
    failed=journal.events('occupation')[-1][1]
    assert failed['reconciliation']=='UNKNOWN'
    assert failed['accounting_as_of']==AT+timedelta(seconds=10)
    assert failed['total']==audit['total']
    assert next(p for p in failed['inventory'] if p['token_id']=='101')['mark_usdc']=='UNKNOWN'
    tick=[0]
    def wait(seconds):
        tick[0]+=seconds
    assert runner.drain(timeout_s=1,monotonic=lambda:tick[0],wait=wait)==1
    residual=journal.events('stop_result')[-1][1]
    assert residual['inventory'][0]['quantity']==D('2')
    assert residual['inventory'][0]['cost_usdc']==D('.80')
    assert residual['inventory'][0]['mark_usdc']=='UNKNOWN'
    assert residual['reconciliation']=='UNKNOWN'


@pytest.mark.parametrize('failed_read',['trades','positions','balance','open-orders'])
def test_stop_attempts_known_cancels_when_reads_fail(tmp_path,failed_read):
    runner,ex,live,journal,obs=world(tmp_path)
    step_at(runner, AT)
    ex.orders['foreign']=ex.orders['order-1'].model_copy(update={'id':'foreign'})
    if failed_read=='trades':
        ex.sdk.list_account_trades.side_effect=TimeoutError('unavailable')
    elif failed_read=='open-orders':
        ex.sdk.list_open_orders.side_effect=TimeoutError('unavailable')
    else:
        original=ex.sdk.get_balance_allowance.side_effect
        def balance(**kwargs):
            if (kwargs['asset_type']=='CONDITIONAL')==(failed_read=='positions'):
                raise TimeoutError('unavailable')
            return original(**kwargs)
        ex.sdk.get_balance_allowance.side_effect=balance
    tick=[0]
    def wait(seconds):
        tick[0]+=seconds
    assert runner.drain(timeout_s=1,monotonic=lambda:tick[0],wait=wait)==1
    assert [c.kwargs['order_id'] for c in ex.sdk.cancel_order.call_args_list]==['order-1','order-2']
    assert ex.orders['foreign'].status=='LIVE'
    assert journal.events('stop_result')[-1][1]['exit_code']==1


@pytest.mark.parametrize('first',['exception','unknown','refused','persistent'])
def test_stop_retries_transient_cancel(tmp_path,first):
    from polymarket.models.clob.cancel import CancelOrdersResponse
    runner,ex,live,journal,obs=world(tmp_path)
    step_at(runner, AT)
    ex.orders['foreign']=ex.orders['order-1'].model_copy(update={'id':'foreign'})
    tick=[0];calls=[]
    runner.clock=lambda:AT+timedelta(seconds=tick[0])
    def cancel(*,order_id):
        calls.append((order_id,tick[0]))
        prior=sum(oid==order_id for oid,at in calls)
        if first=='persistent' or (order_id=='order-1' and prior==1):
            if first in ('exception','persistent'):raise TimeoutError('unavailable')
            return CancelOrdersResponse(canceled=(),not_canceled={order_id:'refused'} if first=='refused' else {})
        return ex.cancel(order_id=order_id)
    ex.sdk.cancel_order.side_effect=cancel
    def wait(seconds):
        tick[0]+=seconds
    result=runner.drain(timeout_s=11,monotonic=lambda:tick[0],wait=wait)
    assert all(oid!='foreign' for oid,at in calls)
    assert ex.orders['foreign'].status=='LIVE'
    if first=='persistent':
        assert result==1
        for oid in ('order-1','order-2'):
            assert [at for key,at in calls if key==oid]==[0,5,10]
    else:
        assert result==0
        assert [at for oid,at in calls if oid=='order-1']==[0,5]
        assert [at for oid,at in calls if oid=='order-2']==[0]
        assert tick[0]==5


@pytest.mark.parametrize('restart',[False,True])
def test_one_order_read_per_round(tmp_path,restart):
    from collections import Counter
    from tests.test_live_sdk import request
    from lp_v2.models import Quote
    from lp_v2.live_runtime import LiveRunner
    runner,ex,live,journal,obs=world(tmp_path)
    terminal=[]
    for i in range(10):
        r=replace(request(),request_id=f'historical-{i}',quote=Quote(CID,'101' if i%2==0 else '102',D('.40'),D('10')))
        oid=live.place_order(r,at=AT).order.order_id
        live.cancel_order(oid,at=AT)
        live.read_orders()
        terminal.append(oid)
    step_at(runner, AT)
    current=[r.order_id for r in live.read_orders() if hasattr(r,'order_id') and r.order_id not in terminal]
    # Actual exchange mutation, not a ledger/internal mock: one partial leg, opposite cancelled.
    yes=next(oid for oid in current if ex.orders[oid].token_id=='101')
    no=next(oid for oid in current if ex.orders[oid].token_id=='102')
    ex.fill(yes,'5','.40',at=AT+timedelta(seconds=1));ex.terminal(no)
    obs.plans=(inputs(no='.60'),)
    ex.sdk.post_order.side_effect=TimeoutError('unknown hedge')
    ex.sdk.get_order.reset_mock()
    step_at(runner, AT+timedelta(seconds=10))
    counts=Counter(c.kwargs['order_id'] for c in ex.sdk.get_order.call_args_list)
    assert all(counts[oid]==0 for oid in terminal)
    assert counts[yes]==counts[no]==1
    assert max(counts.values())<=1
    assert runner.ledger.snapshot().capital.total==D('10')
    assert len([r for r in runner.ledger.snapshot().requests if r.purpose=='hedge'])==1
    if restart:
        journal.close()
        live,journal=adapter(tmp_path,ex.sdk)
        runner=LiveRunner(RunConfig(D('100'),AT+timedelta(seconds=20)),live,obs,clock=lambda:ex.now)
    ex.fill(yes,'1','.40',at=AT+timedelta(seconds=19))
    ex.sdk.get_order.reset_mock()
    step_at(runner, AT+timedelta(seconds=20))
    counts=Counter(c.kwargs['order_id'] for c in ex.sdk.get_order.call_args_list)
    assert all(counts[oid]==0 for oid in (*terminal,no))
    assert counts[yes]==1 and max(counts.values())<=1
    assert runner.ledger.snapshot().positions[0].quantity==D('6')
    assert len(runner.ledger.snapshot().fills)==2
    ex.sdk.get_order.reset_mock()
    assert runner.drain(timeout_s=0)==1  # Unknown hedge remains reserved; cancellation ack is insufficient.
    counts=Counter(c.kwargs['order_id'] for c in ex.sdk.get_order.call_args_list)
    assert all(counts[oid]==0 for oid in (*terminal,no))
    assert max(counts.values())<=1


@pytest.mark.parametrize('reason',['jp-closed','blocked','unknown','orders','trades','positions','balance','region'])
def test_denied_or_failed_round_cancels_owned_orders(tmp_path,reason):
    runner,ex,live,journal,obs=world(tmp_path)
    step_at(runner, AT)
    ex.orders['foreign']=ex.orders['order-1'].model_copy(update={'id':'foreign'})
    ex.cancel_unknown=True
    if reason=='jp-closed':
        obs.region=dict(blocked=True,country='JP');ex.sdk.get_closed_only_mode.return_value=True
    elif reason=='blocked':obs.region=dict(blocked=True,country='GB')
    elif reason=='unknown':obs.region={}
    elif reason=='orders':ex.sdk.get_order.side_effect=TimeoutError('unavailable')
    elif reason=='trades':ex.sdk.list_account_trades.side_effect=TimeoutError('unavailable')
    elif reason in ('positions','balance'):
        original=ex.sdk.get_balance_allowance.side_effect
        def get(**kwargs):
            if (kwargs['asset_type']=='CONDITIONAL')==(reason=='positions'):raise TimeoutError('unavailable')
            return original(**kwargs)
        ex.sdk.get_balance_allowance.side_effect=get
    else:
        def region():raise TimeoutError('unavailable')
        obs.read_region_policy=region
    before=len(ex.posts)
    result=step_at(runner, AT+timedelta(seconds=10))
    assert len(ex.posts)==before
    assert {c.kwargs['order_id'] for c in ex.sdk.cancel_order.call_args_list}=={'order-1','order-2'}
    assert ex.orders['foreign'].status=='LIVE'
    assert runner.ledger.snapshot().capital.total==D('10')
    if reason in ('jp-closed','blocked'):assert result['run']['region_check']=='blocked'
    assert runner.drain(timeout_s=0)==1


def timeout_second_post(ex):
    def post(signed):
        if len(ex.posts) == 1:
            ex.posts.append(signed)
            raise TimeoutError('synthetic secret')
        return ex.post(signed)
    ex.sdk.post_order.side_effect = post


def test_unknown_submission_expiry_restores_rounds_without_replay(tmp_path):
    runner,ex,live,journal,obs = world(tmp_path)
    timeout_second_post(ex)
    step_at(runner, AT)
    original = tuple(p['request'] for _,p in journal.events('intent'))
    assert len(original) == 2 and len(runner.ledger.snapshot().pending) == 1
    for seconds in (10,3719):
        ex.now = AT+timedelta(seconds=seconds)
        obs.plans = tuple(replace(p,books=tuple(replace(b,observed_at=ex.now) for b in p.books)) for p in obs.plans)
        step_at(runner, ex.now)
        assert len(ex.posts) == 2 and runner.ledger.snapshot().capital.total == D('10')
        assert runner.ledger.snapshot().pending == (original[1],)
    assert ex.sdk.cancel_order.call_count >= 1
    ex.now = AT+timedelta(seconds=3730)
    step_at(runner, ex.now)
    state = runner.ledger.snapshot()
    assert not state.pending and len(ex.posts) == 4
    assert {r.reservation_terms.generation for r in state.requests} == {0,1}
    assert state.capital.total <= D('100')
    assert len({p['request'].request_id for _,p in journal.events('intent')}) == 4
    assert all(s.salt != ex.posts[1].salt for s in ex.posts[2:])
    assert 'synthetic secret' not in str(journal.events())
    assert runner.drain(timeout_s=0) == 0


def test_unknown_submission_expiry_survives_restart(tmp_path):
    from lp_v2.live_runtime import LiveRunner
    runner,ex,live,journal,obs = world(tmp_path)
    timeout_second_post(ex)
    step_at(runner, AT)
    original = tuple(p['request'] for _,p in journal.events('intent'))
    journal.close()
    live,journal = adapter(tmp_path,ex.sdk)
    runner = LiveRunner(RunConfig(D('100'),AT),live,obs,clock=lambda:ex.now)
    ex.now = AT+timedelta(seconds=3719)
    obs.plans = tuple(replace(p,books=tuple(replace(b,observed_at=ex.now) for b in p.books)) for p in obs.plans)
    get = ex.sdk.get_order.side_effect
    ex.sdk.get_order.side_effect = TimeoutError('synthetic secret')
    step_at(runner, ex.now)
    assert runner.ledger.snapshot().pending == (original[1],)
    assert runner.ledger.snapshot().capital.total == D('10') and len(ex.posts) == 2
    ex.sdk.get_order.side_effect = get
    journal.close()
    live,journal = adapter(tmp_path,ex.sdk)
    runner = LiveRunner(RunConfig(D('100'),AT),live,obs,clock=lambda:ex.now)
    ex.now += timedelta(seconds=1)
    step_at(runner, ex.now)
    assert not runner.ledger.snapshot().pending and len(ex.posts) == 4
    assert {r.reservation_terms.generation for r in runner.ledger.snapshot().requests} == {0,1}
    step_at(runner, ex.now+timedelta(seconds=10))
    assert len(ex.posts) == 4
    failed = [r for _,r in journal.events('order_result') if r.request == original[1] and r.outcome.value == 'failed']
    assert len(failed) == 1 and failed[0].observed_at == AT+timedelta(seconds=3720)
    assert runner.drain(timeout_s=0) == 0
    assert 'synthetic secret' not in str(journal.events())


@pytest.mark.parametrize('quantity',['0','2','10'])
def test_missing_cancelled_history_drain_succeeds(tmp_path,quantity):
    from polymarket.errors import RequestRejectedError
    runner,ex,live,journal,obs = world(tmp_path)
    step_at(runner, AT)
    oid = next(oid for oid,o in ex.orders.items() if o.token_id == '101')
    ex.orders['foreign'] = ex.orders[oid].model_copy(update={'id':'foreign','maker_address':'0x'+'9'*40})
    if D(quantity):
        ex.fill(oid,quantity,'.40')
    def current(*,order_id):
        row = ex.orders.get(order_id)
        if row is None or row.status != 'LIVE':
            raise RequestRejectedError('absent',status=404)
        return row
    ex.sdk.get_order.side_effect = current
    assert runner.drain(timeout_s=0) == 0
    state = runner.ledger.snapshot()
    assert not state.pending and all(o.authoritative and o.status != 'live' for o in state.orders)
    assert sum((p.quantity for p in state.positions),D('0')) == D(quantity)
    assert state.capital.inventory_cost == D(quantity)*D('.40')
    assert state.cash == D('100')-D(quantity)*D('.40')
    stop = journal.events('stop_result')[-1][1]
    assert stop['reconciliation'] == 'SUCCESS' and stop['exit_code'] == 0
    assert sum((p['quantity'] for p in stop['inventory']),D('0')) == D(quantity)
    assert ex.orders['foreign'].status == 'LIVE'
    assert all(c.kwargs['order_id'] != 'foreign' for c in ex.sdk.cancel_order.call_args_list)


def test_temporary_order_index_gap_keeps_capital_reserved(tmp_path):
    from polymarket.errors import RequestRejectedError
    runner,ex,live,journal,obs = world(tmp_path)
    step_at(runner, AT)
    original = tuple(p['request'] for _,p in journal.events('intent'))
    ex.orders['foreign'] = ex.orders['order-1'].model_copy(update={'id':'foreign','maker_address':'0x'+'9'*40})
    get = ex.sdk.get_order.side_effect
    opens = ex.sdk.list_open_orders.side_effect
    def gap(*,order_id):
        if order_id == 'order-1':
            raise RequestRejectedError('synthetic-secret-never-log',status=404)
        return get(order_id=order_id)
    ex.sdk.get_order.side_effect = gap
    ex.sdk.list_open_orders.side_effect = lambda **kw: (SimpleNamespace(items=tuple(o for oid,o in ex.orders.items() if oid != 'order-1')),)
    ex.cancel_unknown = True
    summary = step_at(runner, AT+timedelta(seconds=10))
    assert summary['accounting']['status'] == 'UNKNOWN'
    assert len(ex.posts) == 2 and runner.ledger.snapshot().capital.total == D('10')
    assert runner.ledger.snapshot().requests == original
    assert all(ex.orders[oid].status == 'LIVE' for oid in ('order-1','order-2'))
    assert not any(r.order and r.order.authoritative and r.order.status != 'live'
                   for _,r in journal.events('order_result'))
    ex.sdk.get_order.side_effect,ex.sdk.list_open_orders.side_effect = get,opens
    ex.cancel_unknown = False
    restored = step_at(runner, AT+timedelta(seconds=20))
    assert restored['accounting']['status'] == 'SUCCESS'
    assert len(ex.posts) == 2 and runner.ledger.snapshot().capital.total == D('10')
    assert all(o.authoritative and o.status == 'live' for o in runner.ledger.snapshot().orders)
    assert all(c.kwargs['order_id'] != 'foreign' for c in ex.sdk.cancel_order.call_args_list)
    assert ex.orders['foreign'].status == 'LIVE' and 'synthetic-secret-never-log' not in str(journal.events())


@pytest.mark.parametrize('mode',['success','transient','persistent','failed-trades','failed-positions','failed-balance'])
def test_drain_cancels_owned_active_despite_terminal_history(tmp_path,mode):
    runner,ex,live,journal,obs = world(tmp_path)
    step_at(runner, AT)
    actual = ex.orders['order-1']
    for oid in tuple(ex.orders):
        ex.terminal(oid)
    runner.ledger.reconcile(live)
    assert all(o.authoritative and o.status == 'cancelled' for o in runner.ledger.snapshot().orders)
    assert live.known_open_order_ids() == ()
    ex.orders['order-1'] = actual
    ex.orders['foreign'] = actual.model_copy(update={'id':'foreign','maker_address':'0x'+'9'*40})
    tick,calls,reads = [0],[],[]
    runner.clock = lambda: AT+timedelta(seconds=tick[0])
    def opened(**kw):
        rows = tuple(o for o in ex.orders.values() if o.status == 'LIVE')
        reads.append((tick[0],tuple(o.id for o in rows)))
        return (SimpleNamespace(items=rows),)
    ex.sdk.list_open_orders.side_effect = opened
    def cancel(*,order_id):
        calls.append((order_id,tick[0]))
        if mode == 'persistent' or (mode == 'transient' and len(calls) == 1):
            raise TimeoutError('synthetic-secret-never-log')
        return ex.cancel(order_id=order_id)
    ex.sdk.cancel_order.side_effect = cancel
    if mode == 'failed-trades':
        ex.sdk.list_account_trades.side_effect = TimeoutError('synthetic-secret-never-log')
    elif mode in ('failed-positions','failed-balance'):
        original = ex.sdk.get_balance_allowance.side_effect
        def balance(**kw):
            if (kw['asset_type'] == 'CONDITIONAL') == (mode == 'failed-positions'):
                raise TimeoutError('synthetic-secret-never-log')
            return original(**kw)
        ex.sdk.get_balance_allowance.side_effect = balance
    def wait(seconds):
        tick[0] += seconds
        assert tick[0] <= 12
    result = runner.drain(timeout_s=11,monotonic=lambda:tick[0],wait=wait)
    assert calls and calls[0] == ('order-1',0)
    assert all(oid == 'order-1' for oid,_ in calls)
    assert ex.orders['foreign'].status == 'LIVE'
    if mode in ('success','transient'):
        assert result == 0 and ex.orders['order-1'].status == 'CANCELED'
        assert [at for _,at in calls] == ([0] if mode == 'success' else [0,5])
        assert 'order-1' not in reads[-1][1] and len(reads) >= 2
        assert all(o.authoritative and o.status != 'live' for o in runner.ledger.snapshot().orders)
    else:
        assert result == 1
        assert journal.events('stop_result')[-1][1]['exit_code'] == 1
        if mode == 'persistent':
            assert [at for _,at in calls] == [0,5,10]
            assert ex.orders['order-1'].status == 'LIVE'
        else:
            assert ex.orders['order-1'].status == 'CANCELED'
            assert journal.events('stop_result')[-1][1]['reconciliation'] == 'UNKNOWN'
    assert 'synthetic-secret-never-log' not in str(journal.events())


def test_fee_policy_change_blocks_new_orders(tmp_path):
    import httpx
    from lp_v2.readonly_sdk import ReadOnlySDK
    from tests.test_readonly_sdk import external_sdk,taker_fee_wire,CID as market_id,AT as public_at
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.journal import Journal
    from tests.test_live_sdk import WALLET,SIGNER
    wire=taker_fee_wire()
    public,_=external_sdk([wire])
    observations=clocked_sdk(public=public,http=httpx.Client(transport=httpx.MockTransport(
        lambda r:httpx.Response(200,json={'in_play':False,'blocked':False,'country':'US'}))))
    ex=Exchange(condition_id=market_id)
    journal=Journal(tmp_path/'journal.sqlite')
    live=LiveSDK(ex.sdk,journal,owner=OWNER,condition_id=market_id,token_ids=TOKENS,wallet=WALLET,signer=SIGNER)
    runner=LiveRunner(RunConfig(D('100'),public_at),live,observations)
    timeout_second_post(ex)
    ex.cancel_unknown=True
    try:
        step_at(runner, public_at)
        before=runner.ledger.snapshot()
        assert len(ex.posts)==2 and len(before.pending)==1 and before.capital.total>D('0')
        wire['feeSchedule']['takerOnly']=False
        step_at(runner, public_at+timedelta(seconds=10))
        after=runner.ledger.snapshot()
        assert len(ex.posts)==2 and after.pending==before.pending
        assert after.capital.total==before.capital.total
        assert after.requests==before.requests
        assert ex.sdk.cancel_order.call_count>=1
        assert {c.kwargs['order_id'] for c in ex.sdk.cancel_order.call_args_list}=={'order-1'}
        assert runner.snapshot()['markets'][0]['eligible'] is False
    finally:
        live.close(); observations.close()


def test_round_uses_completed_market_observation_time(tmp_path,monkeypatch):
    import httpx
    from polymarket import PublicClient
    from lp_v2.readonly_sdk import ReadOnlySDK
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.journal import Journal
    from tests.test_live_sdk import WALLET,SIGNER
    from tests.test_live_cli import timed_preflight_world
    from tests.test_readonly_sdk import CID
    public,seen,current=timed_preflight_world(monkeypatch)
    observations=ReadOnlySDK(public=PublicClient(),http=httpx.Client())
    ex=Exchange(condition_id=CID)
    journal=Journal(tmp_path/'journal.sqlite')
    live=LiveSDK(ex.sdk,journal,owner=OWNER,condition_id=CID,token_ids=TOKENS,wallet=WALLET,signer=SIGNER)
    runner=LiveRunner(RunConfig(D('100'),AT),live,observations,clock=lambda:current[0])
    try:
        summary=runner.step(AT)
        assert len(ex.posts)==2
        assert summary['freshness']['decision_as_of']=='2026-11-02T00:00:03+00:00'
        assert summary['accounting']==dict(status='SUCCESS',as_of=AT.isoformat())
        assert summary['freshness']['wallet_as_of']==AT.isoformat()
        audit=journal.events('occupation')[-1][1]
        assert audit['at']==AT+timedelta(seconds=3) and audit['accounting_as_of']==AT
        assert audit['total']==D('10') and audit['inventory_cost']==0
        assert runner.config.started_at==AT and ex.now==AT
        assert all(r.created_at==AT+timedelta(seconds=3) for r in runner.ledger.snapshot().requests)
        ex.fill('order-1','2','.46',at=AT+timedelta(seconds=5))
        current[0]=AT+timedelta(seconds=10)
        second=runner.step(current[0])
        audit=journal.events('occupation')[-1][1]
        assert second['accounting']['as_of']=='2026-11-02T00:00:10+00:00'
        assert second['freshness']['decision_as_of']=='2026-11-02T00:00:13+00:00'
        assert audit['at']==AT+timedelta(seconds=13)
        assert audit['inventory'][0]['mark_usdc']==D('.94')
        assert audit['inventory'][0]['mark_at']==AT+timedelta(seconds=11)
        assert runner.ledger.snapshot().fills[0].executed_at==AT+timedelta(seconds=5)
    finally:live.close();observations.close();public.close()


@pytest.mark.parametrize('decision', ['rollback2','rollback-half','equal','forward','missing','invalid','naive'])
def test_live_rejects_clock_rollback_after_market_completion(tmp_path,monkeypatch,decision):
    import httpx
    from polymarket import PublicClient
    from lp_v2.readonly_sdk import ReadOnlySDK
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.journal import Journal
    from tests.test_live_sdk import WALLET,SIGNER
    from tests.test_live_cli import timed_preflight_world
    from tests.test_readonly_sdk import CID
    public,seen,current=timed_preflight_world(monkeypatch)
    final={'rollback2':AT+timedelta(seconds=2),'rollback-half':AT+timedelta(milliseconds=500),
           'equal':AT+timedelta(seconds=3),'forward':AT+timedelta(seconds=4),
           'missing':None,'invalid':'unproven','naive':AT.replace(tzinfo=None)}[decision]
    samples=[]
    values=iter([AT+timedelta(seconds=2),AT+timedelta(seconds=3),AT+timedelta(seconds=3),final])
    def clock():
        value=next(values);samples.append(value);return value
    observations=ReadOnlySDK(public=PublicClient(),http=httpx.Client(),clock=clock)
    ex=Exchange(condition_id=CID)
    journal=Journal(tmp_path/'journal.sqlite')
    live=LiveSDK(ex.sdk,journal,owner=OWNER,condition_id=CID,token_ids=TOKENS,wallet=WALLET,signer=SIGNER)
    runner=LiveRunner(RunConfig(D('100'),AT),live,observations,clock=clock)
    try:
        report=runner.step(AT)
        assert samples==[AT+timedelta(seconds=2),AT+timedelta(seconds=3),AT+timedelta(seconds=3),final]
        assert len([url for method,url in seen if '/book?' in url])==2
        valid=decision in ('equal','forward')
        assert len(ex.posts)==(2 if valid else 0)
        assert len(journal.events('signed'))==(2 if valid else 0)
        assert report['accounting']==dict(status='SUCCESS',as_of=AT.isoformat())
        assert runner.config.started_at==AT and ex.now==AT
        assert not runner.ledger.snapshot().fills and not journal.events('merge_intent')
        if valid:
            assert report['freshness']['decision_as_of']==('2026-11-02T00:00:03+00:00' if decision=='equal' else '2026-11-02T00:00:04+00:00')
            assert runner.ledger.snapshot().capital.total==D('10')
        else:
            ex.sdk.create_limit_order.assert_not_called();ex.sdk.post_order.assert_not_called()
            assert journal.events('diagnostic') and not journal.events('intent')
            assert report['freshness']['decision_as_of']=='UNKNOWN'
            assert report['freshness']['income_as_of']=='UNKNOWN'
            assert set(report['markets'][0]['m4'].values())=={'UNKNOWN'}
            assert report['markets'][0]['eligible'] is False
        assert all('observation_completed_at' not in row for row in report['markets'])
    finally:live.close();observations.close();public.close()


@pytest.mark.parametrize('guard', ['expired_absent', 'still_open', 'unexpired', 'incomplete', 'no_cancel'])
def test_expired_absent_cancel_does_not_spin(tmp_path, guard):
    runner, ex, sdk, journal, obs = world(tmp_path)
    step_at(runner, AT)
    if guard != 'no_cancel':
        for oid in sdk.known_order_ids():
            sdk.cancel_order(oid, at=AT)
    if guard != 'still_open':
        ex.orders.clear()
    else:
        for oid, order in tuple(ex.orders.items()):
            ex.orders[oid] = order.model_copy(update={'status': 'LIVE'})
    if guard == 'incomplete':
        ex.sdk.list_open_orders.side_effect = TimeoutError('synthetic secret')
    ex.sdk.get_order.side_effect = TimeoutError('synthetic secret')
    ex.cancel_unknown = True
    start = AT + timedelta(seconds=10 if guard == 'unexpired' else 3720)
    before = len(journal.events('cancel_intent'))
    for seconds in range(0, 601, 10):
        ex.now = start + timedelta(seconds=seconds)
        runner.step(ex.now)
    delta = len(journal.events('cancel_intent')) - before
    if guard in ('expired_absent', 'no_cancel'):
        for oid in sdk.known_order_ids():
            assert sum(p['order_id'] == oid for _, p in journal.events('cancel_intent')[before:]) <= 1
    else:
        assert delta > 1
    assert not sdk.orders_verified()
    assert len(ex.posts) == 2
    assert runner.ledger.snapshot().capital.total == D('10')
    if guard == 'no_cancel':
        assert delta == 2
