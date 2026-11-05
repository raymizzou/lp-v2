from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import sqlite3
from unittest.mock import create_autospec

import pytest
from polymarket import SecureClient
from polymarket.models.clob.orders import SignedOrder
from polymarket.models.clob.order_response import AcceptedOrder, RejectedOrder
from lp_v2.models import OrderRequest, Quote, ReservationTerms, Outcome

AT = datetime(2026, 11, 2, tzinfo=UTC)
CID = '0x' + 'a' * 64
WALLET = '0x' + '1' * 40
SIGNER = '0x' + '2' * 40
TOKENS = ('101', '102')
OWNER = 'lp-v2:test'


def request():
    return OrderRequest('req-1', OWNER, Quote(CID, TOKENS[0], D('.40'), D('10')), AT,
                        AT + timedelta(hours=1), ReservationTerms(TOKENS, D('10'), (D('0'), D('0'))))


def boundary():
    sdk = create_autospec(SecureClient, instance=True, spec_set=True)
    raw_order_context(sdk)
    sdk.create_limit_order.return_value = SignedOrder(builder='0x'+'0'*64, expiration=int((AT+timedelta(hours=1)).timestamp()),
        maker=WALLET, maker_amount=4000000, metadata='0x'+'0'*64, order_type='GTD', salt=123,
        side='BUY', signature='0x'+'0'*130, signature_type=2, signer=SIGNER, taker_amount=10000000,
        timestamp=int(AT.timestamp()*1000), token_id=TOKENS[0], post_only=True)
    sdk.get_order.side_effect = KeyError('unavailable')
    sdk.post_order.return_value = AcceptedOrder(order_id='0x083de447758da764497f6ef7506bd6130182da09d47f60b5660d59d2bba90c12', status='live', making_amount=D('0'), taking_amount=D('0'),trade_ids=(),transactions_hashes=())
    return sdk


def adapter(tmp_path, sdk=None):
    from lp_v2.journal import Journal
    from lp_v2.live_sdk import LiveSDK
    j = Journal(tmp_path / 'journal.sqlite')
    return LiveSDK(sdk or boundary(), j, owner=OWNER, condition_id=CID, token_ids=TOKENS,
                   wallet=WALLET, signer=SIGNER, clock=lambda: AT), j


def test_read_cycle_history_tracks_appended_facts(tmp_path):
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.models import OrderResult
    from tests.fakes.live_exchange import Exchange
    ex=Exchange()
    live,journal=adapter(tmp_path,ex.sdk)
    other=replace(request(),request_id='second',quote=Quote(CID,'102',D('.50'),D('10')))
    pending=replace(request(),request_id='pending')
    with live.read_cycle(now=AT):
        first=live.place_order(request(),at=AT)
        assert live.known_order_ids()==(first.order.order_id,)
        assert live.place_order(request(),at=AT)==first and len(ex.posts)==1
        live.read_orders()
        live.cancel_order(first.order.order_id,at=AT)
        terminal=live.read_orders()[0]
        assert terminal.authoritative and terminal.status=='cancelled'
        assert live.known_open_order_ids()==()
        second=live.place_order(other,at=AT)
        assert set(live.known_order_ids())=={first.order.order_id,second.order.order_id}
        journal.append('intent',dict(request=pending,hedge_ceiling=D('.58')))
        facts=live.read_orders()
        assert len(facts)==3 and facts[-1].request==pending and facts[-1].outcome==Outcome.UNKNOWN
        assert not live.orders_verified()
        journal.append('order_result',OrderResult(pending,Outcome.FAILED,None,AT))
        with live.read_cycle(now=AT):
            assert live.read_orders()[-1].outcome==Outcome.FAILED and live.orders_verified()
    ex.now=AT+timedelta(seconds=10)
    restarted=LiveSDK(ex.sdk,journal,owner=OWNER,condition_id=CID,token_ids=TOKENS,wallet=WALLET,signer=SIGNER,clock=lambda:ex.now)
    with restarted.read_cycle(now=ex.now):
        facts=restarted.read_orders()
        assert len(facts)==3 and facts[0]==terminal
        assert facts[1].observed_at==ex.now and facts[1].authoritative
        assert facts[2].outcome==Outcome.FAILED
    assert len(ex.posts)==2
    journal.close()


def test_intent_is_durable_before_submission(tmp_path):
    sdk = boundary()
    live, journal = adapter(tmp_path, sdk)
    def submit(signed):
        with sqlite3.connect(journal.path) as db:
            rows = db.execute('SELECT kind, payload FROM events ORDER BY seq').fetchall()
        assert rows[0][0] == 'intent'
        from lp_v2.journal import decode
        payload = decode(rows[0][1])
        assert payload['request'] == request()
        assert payload['hedge_ceiling'] == D('.58')
        assert rows[1][0] == 'signed'
        return sdk.post_order.return_value
    sdk.post_order.side_effect = submit
    assert live.place_order(request(), at=AT).outcome == Outcome.SUCCESS
    assert journal.events()[0][1]['request'] == request()
    journal.append('observation', {'at': AT, 'occupation': D('10')})
    assert len(journal.events()) == 4


@pytest.mark.parametrize('mode,expected', [('success', Outcome.SUCCESS), ('rejected', Outcome.FAILED), ('unknown', Outcome.UNKNOWN)])
def test_post_only_gtd_outcomes(tmp_path, mode, expected):
    sdk = boundary()
    if mode == 'rejected':
        sdk.post_order.return_value = RejectedOrder(code='post_only_would_cross', message='unsafe secret transport detail')
    if mode == 'unknown':
        sdk.post_order.side_effect = TimeoutError('unsafe secret transport detail')
    live, journal = adapter(tmp_path, sdk)
    result = live.place_order(request(), at=AT)
    assert result.outcome == expected
    kwargs = sdk.create_limit_order.call_args.kwargs
    assert kwargs == dict(token_id='101', price=D('.40'), size=D('10'), side='BUY', post_only=True,
                          expiration=int(AT.timestamp())+3600)
    assert sdk.post_order.call_count == 1
    assert 'unsafe secret' not in str(journal.events())
    before = sdk.post_order.call_count
    for alien in (replace(request(), owner='foreign'), replace(request(), quote=Quote('alien','101',D('.4'),D('10'))),
                  replace(request(), quote=Quote(CID,'999',D('.4'),D('10')))):
        with pytest.raises(ValueError):
            live.place_order(alien, at=AT)
    assert sdk.post_order.call_count == before


@pytest.mark.parametrize('where', ['before-send', 'after-send'])
def test_crash_recovery_never_resubmits(tmp_path, where):
    sdk = boundary()
    class Crash(BaseException):
        pass
    if where == 'before-send':
        sdk.create_limit_order.side_effect = Crash()
    else:
        sdk.post_order.side_effect = Crash()
    live, journal = adapter(tmp_path, sdk)
    with pytest.raises(Crash):
        live.place_order(request(), at=AT)
    journal.close()
    restarted, reopened = adapter(tmp_path, sdk)
    before = sdk.post_order.call_count
    result = restarted.place_order(request(), at=AT)
    assert result.outcome == Outcome.UNKNOWN and result.order is None
    assert sdk.post_order.call_count == before
    facts = restarted.read_orders()
    assert facts == (result,)
    from lp_v2.ledger import Ledger
    ledger = Ledger(OWNER, D('100'))
    # Recovery facts must recreate the reservation even without an exchange id.
    sdk.list_account_trades.return_value = ()
    from polymarket.models.clob.account import BalanceAllowance
    sdk.get_balance_allowance.side_effect = lambda *, asset_type, token_id=None: BalanceAllowance(balance=100000000 if asset_type == 'COLLATERAL' else 0, allowances={})
    ledger.reconcile(restarted)
    assert ledger.snapshot().pending == (request(),)
    assert ledger.snapshot().capital.total == D('10')
    if where == 'after-send':
        signed = reopened.events('signed')[0][1]
        assert len(signed['order_ids']) == 2
        assert all(value.startswith('0x') and len(value) == 66 for value in signed['order_ids'])


@pytest.mark.parametrize('mode', ['success', 'unknown', 'late-fill'])
def test_cancel_and_late_fill(tmp_path, mode):
    from tests.fakes.live_exchange import Exchange
    from lp_v2.ledger import Ledger
    exchange = Exchange()
    live, journal = adapter(tmp_path, exchange.sdk)
    result = live.place_order(request(), at=AT)
    oid = result.order.order_id
    exchange.reject_tokens.add('102')
    live.place_order(replace(request(), request_id='req-2', quote=Quote(CID,'102',D('.50'),D('10'))), at=AT)
    exchange.orders['foreign'] = exchange.orders[oid].model_copy(update={'id':'foreign','maker_address':SIGNER})
    with pytest.raises(ValueError):
        live.cancel_order('foreign', at=AT)
    exchange.cancel_unknown = mode == 'unknown'
    if mode == 'late-fill':
        exchange.fill(oid,'2','.40')
    result = live.cancel_order(oid, at=AT+timedelta(seconds=3))
    assert result.outcome == (Outcome.UNKNOWN if mode == 'unknown' else Outcome.SUCCESS)
    ledger = Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    first = ledger.snapshot()
    ledger.reconcile(live)
    assert ledger.snapshot() == first
    if mode == 'unknown':
        assert first.capital.total == D('10')
        exchange.terminal(oid)
        ledger.reconcile(live)
        assert ledger.snapshot().capital.total == 0
    elif mode == 'late-fill':
        assert first.capital.inventory_cost == D('.80')
        assert first.positions[0].quantity == D('2')
        assert len(first.fills) == 1
    else:
        assert first.capital.total == 0
    assert exchange.sdk.cancel_order.call_count == 1
    assert exchange.orders['foreign'].status == 'LIVE'


def test_wire_expiry_second_precision(tmp_path):
    from tests.fakes.live_exchange import Exchange
    exchange = Exchange()
    live,_ = adapter(tmp_path,exchange.sdk)
    r = replace(request(),created_at=AT+timedelta(microseconds=123456),expires_at=AT+timedelta(hours=1,microseconds=123456))
    live.place_order(r,at=r.created_at)
    fact = live.read_orders()[0]
    assert fact.request == r
    assert fact.authoritative
    exchange.orders[fact.order_id] = exchange.orders[fact.order_id].model_copy(update={'expires_at':AT+timedelta(hours=1,seconds=1)})
    with pytest.raises(ValueError,match='identity'):
        live.read_orders()


def test_rejected_cancel_readback_reconciles(tmp_path):
    from tests.fakes.live_exchange import Exchange
    from lp_v2.ledger import Ledger
    from polymarket.models.clob.cancel import CancelOrdersResponse
    exchange = Exchange()
    live,journal = adapter(tmp_path,exchange.sdk)
    order = live.place_order(request(),at=AT).order
    exchange.sdk.cancel_order.side_effect = lambda **kw: CancelOrdersResponse(canceled=(),not_canceled={order.order_id:'refused'})
    assert live.cancel_order(order.order_id,at=AT+timedelta(seconds=1)).outcome == Outcome.FAILED
    exchange.fill(order.order_id,'10','.40')
    ledger = Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    assert ledger.snapshot().orders[0].status == 'filled'
    assert ledger.snapshot().orders[0].authoritative
    assert ledger.snapshot().positions[0].quantity == D('10')
    # A placement rejection still means no order and cannot be replayed.
    exchange.reject_tokens.add('102')
    rejected = replace(request(),request_id='req-2',quote=Quote(CID,'102',D('.5'),D('10')))
    assert live.place_order(rejected,at=AT).outcome == Outcome.FAILED
    count = len(exchange.posts)
    assert live.place_order(rejected,at=AT).outcome == Outcome.FAILED
    assert len(exchange.posts) == count


def test_hedge_ceiling_uses_opposite_average_cost(tmp_path):
    from tests.fakes.live_exchange import Exchange
    exchange = Exchange()
    live,journal = adapter(tmp_path,exchange.sdk)
    terms = ReservationTerms(TOKENS,D('10'),(D('.01'),D('.01')))
    opening = replace(request(),reservation_terms=terms)
    live.place_order(opening,at=AT)
    exchange.fill('order-1','10','.40')
    hedge = replace(opening,request_id='hedge-1',quote=Quote(CID,'102',D('.50'),D('10')),
                    purpose='hedge',created_at=AT+timedelta(seconds=10),expires_at=AT+timedelta(hours=1,seconds=10))
    live.place_order(hedge,at=hedge.created_at)
    record = [p for _,p in journal.events('intent') if p['request'].request_id == 'hedge-1'][0]
    assert record['hedge_ceiling'] == D('.57')
    with pytest.raises(ValueError,match='ceiling'):
        live.place_order(replace(hedge,request_id='bad-hedge',quote=Quote(CID,'102',D('.58'),D('10'))),at=hedge.created_at)
    assert len(exchange.posts) == 2


@pytest.mark.parametrize('mode', ['confirmed','failed','timeout','no-receipt','wrong-receipt','ack','unrelated-cash'])
def test_merge_receipt_and_cash_gate(tmp_path, mode):
    from eth_abi import encode
    from eth_utils import keccak
    from polymarket.environments import PRODUCTION as env
    from polymarket.transactions import SyncGaslessTransactionHandle
    from polymarket.clients._transport import SyncTransport
    from polymarket._internal.eoa.rpc import SyncJsonRpcClient
    from tests.fakes.live_exchange import Exchange
    from lp_v2.models import MergeRequest
    from lp_v2.ledger import Ledger
    exchange = Exchange()
    live,journal = adapter(tmp_path,exchange.sdk)
    rpc = create_autospec(SyncJsonRpcClient,instance=True,spec_set=True)
    relay = create_autospec(SyncTransport,instance=True,spec_set=True)
    live.rpc,live.relayer = rpc,relay
    txhash = '0x'+'c'*64
    exchange.sdk.merge_positions.return_value = SyncGaslessTransactionHandle('relay-1',txhash,relay,1,0)
    relay.get_json.return_value = dict(state='STATE_NEW' if mode == 'ack' else 'STATE_CONFIRMED',transaction_id='relay-1',transaction_hash=txhash)
    receipt = receipt_fixture()
    receipt.update(status='0x0' if mode == 'failed' else '0x1',transactionHash=txhash)
    if mode == 'unrelated-cash':
        receipt['logs'].pop(0)  # Cash growth without correlated owned-token outflow is not proof.
    if mode == 'wrong-receipt':
        receipt['transactionHash'] = '0x'+'d'*64
    rpc.eth_get_transaction_receipt.return_value = None if mode == 'no-receipt' else receipt
    if mode == 'timeout':
        relay.get_json.side_effect = TimeoutError('pending')
    live.place_order(replace(request(),quote=Quote(CID,'101',D('.40'),D('2')),reservation_terms=ReservationTerms(TOKENS,D('2'),(D('0'),D('0')))),at=AT)
    live.place_order(replace(request(),request_id='req-2',quote=Quote(CID,'102',D('.50'),D('2')),reservation_terms=ReservationTerms(TOKENS,D('2'),(D('0'),D('0')))),at=AT)
    exchange.fill('order-1','2','.40')
    exchange.fill('order-2','2','.50')
    ledger = Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    merge = MergeRequest('merge-1',OWNER,CID,*TOKENS,D('2'),AT+timedelta(seconds=5))
    ledger.register_merge(merge)
    # Acknowledgement is durable before any cash can be considered settled.
    first = live.merge(merge,at=merge.created_at)
    assert first.received_usdc == 0
    assert ledger.snapshot().capital.total == D('2')
    if mode in ('confirmed','unrelated-cash'):
        exchange.cash += D('2')
        if mode == 'confirmed':
            exchange.quantities = {'101':D('0'),'102':D('0')}
    if mode == 'confirmed':
        ledger.reconcile(live)
        assert ledger.snapshot().realized_profit == D('.20')
        assert ledger.snapshot().capital.total == 0
        assert ledger.snapshot().cash == D('100.20')
    else:
        result = live.read_balance().merge_results[0]
        assert result.received_usdc == 0
        assert result.outcome == (Outcome.FAILED if mode == 'failed' else Outcome.UNKNOWN)
    live.merge(merge,at=merge.created_at)
    assert exchange.sdk.merge_positions.call_count == 1
    assert exchange.sdk.merge_positions.call_args.kwargs == dict(condition_id=CID,amount=2000000,metadata='merge-1')
    journal.close()
    restored,reopened = adapter(tmp_path,exchange.sdk)
    restored.rpc,restored.relayer = rpc,relay
    restored.merge(merge,at=merge.created_at)
    assert exchange.sdk.merge_positions.call_count == 1


def receipt_fixture(shape='standard'):
    from pathlib import Path
    import json
    return json.loads((Path(__file__).parent/'data'/'m4_merge_receipts'/f'{shape}.json').read_text())['receipt']


def merge_world(tmp_path,receipt,*,yes_quantity='2'):
    from polymarket.transactions import SyncGaslessTransactionHandle
    from polymarket.clients._transport import SyncTransport
    from polymarket._internal.eoa.rpc import SyncJsonRpcClient
    from tests.fakes.live_exchange import Exchange
    from lp_v2.models import MergeRequest
    from lp_v2.ledger import Ledger
    ex=Exchange()
    live,journal=adapter(tmp_path,ex.sdk)
    rpc=create_autospec(SyncJsonRpcClient,instance=True,spec_set=True)
    relay=create_autospec(SyncTransport,instance=True,spec_set=True)
    live.rpc,live.relayer=rpc,relay
    tx=receipt['transactionHash']
    ex.sdk.merge_positions.return_value=SyncGaslessTransactionHandle('relay-1',tx,relay,1,0)
    relay.get_json.return_value=dict(state='STATE_NEW',transaction_id='relay-1',transaction_hash=tx)
    rpc.eth_get_transaction_receipt.return_value=receipt
    terms=ReservationTerms(TOKENS,D(yes_quantity),(D('0'),D('0')))
    for i,token,price,quantity in [(1,'101','.40',yes_quantity),(2,'102','.50','2')]:
        live.place_order(replace(request(),request_id=f'pair-{i}',quote=Quote(CID,token,D(price),D(quantity)),reservation_terms=terms),at=AT)
        ex.fill(f'order-{i}','2',price)
    ledger=Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    merge=MergeRequest('merge-1',OWNER,CID,*TOKENS,D('2'),AT+timedelta(seconds=5))
    assert ledger.register_merge(merge)
    assert live.merge(merge,at=merge.created_at).received_usdc==0
    return live,journal,ex,ledger,merge,rpc,relay


@pytest.mark.parametrize('shape',['standard','neg-risk'])
@pytest.mark.parametrize('fault',[None,'status','hash','emitter','wallet','token','amount','missing-credit','missing-outflow'])
def test_merge_real_event_shapes(tmp_path,shape,fault):
    from eth_abi import encode
    receipt=receipt_fixture(shape)
    live,journal,ex,ledger,merge,rpc,relay=merge_world(tmp_path,receipt)
    if fault=='status':receipt['status']='0x0'
    if fault=='hash':receipt['transactionHash']='0x'+'d'*64
    if fault=='emitter':receipt['logs'][0]['address']=SIGNER
    if fault=='wallet':receipt['logs'][0]['topics'][2]='0x'+SIGNER[2:].zfill(64)
    if fault=='token':receipt['logs'][0]['data']='0x'+encode(['uint256[]','uint256[]'],[[101,999],[2000000,2000000]]).hex()
    if fault=='amount':receipt['logs'][0]['data']='0x'+encode(['uint256[]','uint256[]'],[[101,102],[1000000,2000000]]).hex()
    if fault=='missing-credit':receipt['logs'].pop()
    if fault=='missing-outflow':receipt['logs'].pop(0)
    relay.get_json.return_value['state']='STATE_CONFIRMED'
    # Real token/cash readbacks reflect the merge; receipt proof must still match.
    ex.cash+=D('2');ex.quantities={'101':D('0'),'102':D('0')}
    if fault is None:
        ledger.reconcile(live)
        assert ledger.snapshot().realized_profit==D('.20')
        assert ledger.snapshot().capital.total==0
        assert ledger.snapshot().cash==D('100.20')
        before=ledger.snapshot();ledger.reconcile(live)
        assert ledger.snapshot()==before
        journal.close()
        restored,reopened=adapter(tmp_path,ex.sdk)
        restored.rpc,restored.relayer=rpc,relay
        restored.merge(merge,at=merge.created_at)
        assert restored.read_balance().merge_results[0].received_usdc==D('2')
    else:
        result=live.read_balance().merge_results[0]
        assert result.received_usdc==0
        assert result.outcome==(Outcome.FAILED if fault=='status' else Outcome.UNKNOWN)
        assert ledger.snapshot().capital.total==D('2')
    assert ex.sdk.merge_positions.call_count==1


@pytest.mark.parametrize('proof',['valid','missing','wrong'])
def test_merge_confirmation_with_concurrent_wallet_changes(tmp_path,proof):
    receipt=receipt_fixture()
    live,journal,ex,ledger,merge,rpc,relay=merge_world(tmp_path,receipt,yes_quantity='4')
    ex.fill('order-1','1','.39',at=AT+timedelta(seconds=6))
    ex.cash+=D('2.70')  # Exact merge payout2 plus independent reward0.70, less fill0.39.
    ex.quantities={'101':D('1'),'102':D('0')}
    relay.get_json.return_value['state']='STATE_CONFIRMED'
    if proof=='missing':rpc.eth_get_transaction_receipt.return_value=None
    if proof=='wrong':receipt['transactionHash']='0x'+'d'*64
    if proof=='valid':
        ledger.reconcile(live)
        state=ledger.snapshot()
        assert state.cash==D('100.51')
        assert state.realized_profit==D('.20')
        positions={p.token_id:p for p in state.positions}
        assert positions['101'].quantity==D('1') and positions['101'].cost_usdc==D('.39')
        assert positions['102'].quantity==0 and positions['102'].cost_usdc==0
        assert state.capital.total==D('2')
        assert state.merges[0].received_usdc==D('2')
        assert tuple(p.cost_usdc for p in state.merges[0].consumed_positions)==(D('.80'),D('1.00'))
        ledger.reconcile(live)
        assert ledger.snapshot()==state
    else:
        assert live.read_balance().merge_results[0].received_usdc==0
        with pytest.raises(ValueError,match='position'):
            ledger.reconcile(live)
        assert ledger.snapshot().capital.total==D('4')
    live.merge(merge,at=merge.created_at)
    assert ex.sdk.merge_positions.call_count==1


@pytest.mark.parametrize('failure',['preparation','identity','post-timeout'])
def test_pre_submission_failure_is_definitive(tmp_path,failure):
    from tests.fakes.live_exchange import Exchange
    from lp_v2.ledger import Ledger
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.cli import RunConfig
    from tests.test_live_runtime import Observations,inputs
    ex=Exchange();live,journal=adapter(tmp_path,ex.sdk)
    if failure=='preparation':
        ex.sdk.create_limit_order.side_effect=ValueError('synthetic-secret-never-log')
    elif failure=='identity':
        ex.sdk.create_limit_order.side_effect=lambda **kw:replace(ex.sign(**kw),token_id='999')
    else:
        ex.sdk.post_order.side_effect=TimeoutError('synthetic-secret-never-log')
    results=[]
    for r in (request(),replace(request(),request_id='req-2',quote=Quote(CID,'102',D('.50'),D('10')))):
        results.append(live.place_order(r,at=AT))
        live.place_order(r,at=AT)
    expected=Outcome.UNKNOWN if failure=='post-timeout' else Outcome.FAILED
    assert all(r.outcome==expected for r in results)
    assert ex.sdk.post_order.call_count==(2 if failure=='post-timeout' else 0)
    facts=str(journal.events())
    assert 'synthetic-secret-never-log' not in facts
    if failure!='post-timeout':assert '未提交' in facts
    ledger=Ledger(OWNER,D('100'));ledger.reconcile(live)
    assert ledger.snapshot().capital.total==(D('10') if failure=='post-timeout' else 0)
    assert bool(ledger.snapshot().pending)==(failure=='post-timeout')
    runner=LiveRunner(RunConfig(D('100'),AT),live,Observations((inputs(),)),clock=lambda:AT)
    assert runner.drain(timeout_s=0)==(1 if failure=='post-timeout' else 0)


@pytest.mark.parametrize('status',['DELAYED','UNMATCHED','404','unavailable'])
def test_nonterminal_statuses_and_missing_history(tmp_path,status):
    from polymarket.errors import RequestRejectedError
    from tests.fakes.live_exchange import Exchange
    from lp_v2.ledger import Ledger
    ex=Exchange();live,journal=adapter(tmp_path,ex.sdk)
    oid=live.place_order(request(),at=AT).order.order_id
    ex.reject_tokens.add('102')
    live.place_order(replace(request(),request_id='opposite-rejected',quote=Quote(CID,'102',D('.50'),D('10'))),at=AT)
    ex.fill(oid,'2','.40',at=AT+timedelta(seconds=1))
    ledger=Ledger(OWNER,D('100'));ledger.reconcile(live)
    original=ex.orders[oid]
    if status in ('DELAYED','UNMATCHED'):
        ex.orders[oid]=original.model_copy(update={'status':status,'size_matched':D('0')})
    else:
        ex.sdk.get_order.side_effect=RequestRejectedError('synthetic-secret-never-log',status=404) if status=='404' else TimeoutError('synthetic-secret-never-log')
    fact=next(o for o in live.read_orders() if hasattr(o,'order_id'))
    assert fact.request==request() and fact.order_id==oid
    assert fact.filled_quantity==D('2') and fact.remaining_quantity==D('8')
    assert fact.status=='live' and fact.authoritative is (status=='404')
    assert live.orders_verified() is (status=='404')
    count=len(ex.posts)
    assert live.place_order(request(),at=AT).outcome==(Outcome.SUCCESS if status=='404' else Outcome.UNKNOWN)
    assert len(ex.posts)==count
    ledger.reconcile(live)
    assert ledger.snapshot().capital.total==D('10')
    assert 'synthetic-secret-never-log' not in str(journal.events())
    assert journal.events('diagnostic')
    ex.sdk.get_order.side_effect=lambda *,order_id:ex.orders[order_id]
    ex.orders[oid]=original
    ex.fill(oid,'1','.40',at=AT+timedelta(seconds=3))
    ledger.reconcile(live)
    assert ledger.snapshot().positions[0].quantity==D('3')
    assert len(ledger.snapshot().fills)==2
    live.cancel_order(oid,at=AT+timedelta(seconds=4))
    ledger.reconcile(live)
    assert ledger.snapshot().orders[0].authoritative
    assert ledger.snapshot().orders[0].status=='cancelled'
    assert ledger.snapshot().capital.total==D('3')
    before=ledger.snapshot();ledger.reconcile(live)
    assert ledger.snapshot()==before


@pytest.mark.parametrize('matching',[False,True])
def test_accepted_order_hash_diagnostic(tmp_path,matching):
    # Literal standard EIP712 oracle over the fixed boundary SignedOrder fixture.
    expected='0x083de447758da764497f6ef7506bd6130182da09d47f60b5660d59d2bba90c12'
    sdk=boundary()
    actual=expected if matching else 'unexpected-owned-order'
    sdk.post_order.return_value=sdk.post_order.return_value.model_copy(update={'order_id':actual})
    live,journal=adapter(tmp_path,sdk)
    result=live.place_order(request(),at=AT)
    assert result.outcome==Outcome.SUCCESS and result.order.order_id==actual
    warnings=[p for _,p in journal.events('diagnostic') if p['reason']=='accepted order id mismatch']
    assert len(warnings)==(0 if matching else 1)
    if warnings:assert warnings[0]['request_id']==request().request_id and warnings[0]['order_id']==actual
    assert expected in journal.events('signed')[0][1]['order_ids']
    assert live.place_order(request(),at=AT)==result
    journal.close()
    recovered,reopened=adapter(tmp_path,sdk)
    assert recovered.place_order(request(),at=AT)==result
    assert sdk.post_order.call_count==1


def timed_out_intent(tmp_path):
    from tests.fakes.live_exchange import Exchange
    from polymarket.errors import RequestRejectedError
    ex = Exchange()
    ex.sdk.post_order.side_effect = TimeoutError('synthetic secret')
    live, journal = adapter(tmp_path, ex.sdk)
    live.clock = lambda: ex.now
    live.place_order(request(), at=AT)
    ids = journal.events('signed')[0][1]['order_ids']
    ex.sdk.get_order.side_effect = RequestRejectedError('synthetic secret', status=404)
    ex.reject_tokens.add('102')
    ex.sdk.post_order.side_effect = ex.post
    live.place_order(replace(request(),request_id='opposite',quote=Quote(CID,'102',D('.5'),D('10'))),at=AT)
    return ex, live, journal, ids


@pytest.mark.parametrize('proof', ['complete','missing-signed','transport','active','open-error','trade-error','malformed-open'])
def test_expired_unknown_requires_complete_absence(tmp_path, proof):
    from lp_v2.ledger import Ledger
    ex, live, journal, ids = timed_out_intent(tmp_path)
    if proof == 'missing-signed':
        # A durable intent without signing proof can result from a pre-send crash.
        journal.close()
        tmp_path.joinpath('journal.sqlite').unlink()
        live, journal = adapter(tmp_path, ex.sdk)
        live.clock = lambda: ex.now
        journal.append('intent',dict(request=request()))
    elif proof == 'transport':
        from polymarket.errors import RequestRejectedError
        def get(*,order_id):
            if order_id == ids[0]:
                raise TimeoutError('synthetic secret')
            raise RequestRejectedError('absent',status=404)
        ex.sdk.get_order.side_effect = get
    elif proof == 'active':
        ex.post(ex.sign(token_id='101',price=D('.4'),size=D('10'),expiration=int(request().expires_at.timestamp())))
        row = next(iter(ex.orders.values())).model_copy(update={'id':ids[1]})
        ex.sdk.get_order.side_effect = lambda **kw: row if kw['order_id'] == ids[1] else None
    elif proof == 'malformed-open':
        from types import SimpleNamespace
        from polymarket.models.clob.account import OpenOrder
        ex.post(ex.sign(token_id='101',price=D('.4'),size=D('10'),expiration=int(request().expires_at.timestamp())))
        row = next(iter(ex.orders.values()))
        malformed = OpenOrder.model_validate(dict(row.model_dump(by_alias=True),id=''))
        ex.sdk.list_open_orders.side_effect = lambda **kw: (SimpleNamespace(items=()),SimpleNamespace(items=(malformed,)))
    elif proof == 'open-error':
        ex.sdk.list_open_orders.side_effect = TimeoutError('synthetic secret')
    elif proof == 'trade-error':
        ex.sdk.list_account_trades.side_effect = TimeoutError('synthetic secret')
    ex.now = request().expires_at+timedelta(seconds=119)
    assert not live.orders_verified()
    facts = live.read_orders()
    assert not any(getattr(f,'outcome',None) == Outcome.FAILED and f.request.request_id == 'req-1' for f in facts)
    ledger = Ledger(OWNER,D('100'))
    if proof not in ('trade-error',):
        ledger.reconcile(live)
        assert ledger.snapshot().capital.total == D('10')
    ex.now += timedelta(seconds=1)
    facts = live.read_orders()
    fact = next(f for f in facts if f.request.request_id == 'req-1')
    if proof == 'complete':
        assert fact.outcome == Outcome.FAILED and fact.order is None
        assert journal.events('diagnostic')[-1][1]['reason'] == '到期未见'
        ledger.reconcile(live)
        assert ledger.snapshot().capital.total == 0
        journal.close()
        live, journal = adapter(tmp_path, ex.sdk)
        before = ex.sdk.post_order.call_count
        assert live.place_order(request(),at=AT).outcome == Outcome.FAILED
        assert ex.sdk.post_order.call_count == before
    else:
        assert getattr(fact,'outcome',None) != Outcome.FAILED
        if proof == 'malformed-open':
            ledger.reconcile(live)
            assert ledger.snapshot().pending == (request(),)
            assert ledger.snapshot().capital.total == D('10')
            assert all(o.status == 'LIVE' for o in ex.orders.values())
    assert 'synthetic secret' not in str(journal.events())


@pytest.mark.parametrize('index,bad', [(0,False),(1,False),(1,True)])
def test_timeout_recovers_by_either_signed_hash(tmp_path,index,bad):
    from polymarket.errors import RequestRejectedError
    ex, live, journal, ids = timed_out_intent(tmp_path)
    ex.post(ex.sign(token_id='101',price=D('.4'),size=D('10'),expiration=int(request().expires_at.timestamp())))
    row = next(iter(ex.orders.values())).model_copy(update={'id':ids[index], **({'token_id':'999'} if bad else {})})
    def get(*,order_id):
        if order_id == ids[index]:
            return row
        raise RequestRejectedError('absent',status=404)
    ex.sdk.get_order.side_effect = get
    ex.now = request().expires_at+timedelta(seconds=120)
    if bad:
        with pytest.raises(ValueError,match='identity'):
            live.read_orders()
        assert not live.orders_verified()
    else:
        fact = live.read_orders()[0]
        assert fact.order_id == ids[index] and fact.request == request() and fact.authoritative
        assert fact.remaining_quantity == D('10')
        journal.close()
        live, journal = adapter(tmp_path,ex.sdk)
        before = ex.sdk.post_order.call_count
        assert live.place_order(request(),at=AT).order.order_id == ids[index]
        assert live.read_orders()[0].order_id == ids[index]
        assert ex.sdk.post_order.call_count == before
    assert 'synthetic secret' not in str(journal.events())


@pytest.mark.parametrize('mode',['valid','foreign','wallet','token','market','side','fee','unsettled','quantity','conflict'])
def test_unresolved_signed_hash_claims_trades(tmp_path,mode):
    from lp_v2.ledger import Ledger
    ex, live, journal, ids = timed_out_intent(tmp_path)
    ex.post(ex.sign(token_id='101',price=D('.4'),size=D('10'),expiration=int(request().expires_at.timestamp())))
    row = ex.orders.pop('order-2')
    ex.orders[ids[0]] = row.model_copy(update={'id':ids[0]})
    ex.fill(ids[0],'2','.40')
    ex.terminal(ids[0])
    raw = ex.trades[0]
    changes = {'wallet':{'maker_address':SIGNER},'token':{'token_id':'999'},'side':{'side':'SELL'},
               'fee':{'fee_rate_bps':D('1')},'quantity':{'matched_amount':D('11')},'foreign':{'order_id':'foreign'}}
    if mode in changes:
        ex.trades[0] = raw.model_copy(update={'maker_orders':(raw.maker_orders[0].model_copy(update=changes[mode]),)})
    elif mode == 'market':
        ex.trades[0] = raw.model_copy(update={'condition_id':'foreign'})
    elif mode == 'unsettled':
        ex.trades[0] = raw.model_copy(update={'status':'MINED'})
    elif mode == 'conflict':
        ex.trades.append(raw.model_copy(update={'id':'conflict','maker_orders':(raw.maker_orders[0].model_copy(update={'order_id':ids[1]}),)}))
    if mode not in ('valid','foreign'):
        with pytest.raises(ValueError):
            live.read_trades()
        assert not live.orders_verified()
        return
    fills = live.read_trades()
    if mode == 'foreign':
        assert fills == ()
        assert live.read_orders()[0].outcome == Outcome.UNKNOWN
        return
    assert len(fills) == 1 and fills[0].order_id == ids[0] and fills[0].quantity == D('2')
    early = Ledger(OWNER,D('100'))
    early.reconcile(live)
    assert early.snapshot().orders[0].filled_quantity == D('2')
    assert not early.snapshot().orders[0].authoritative and early.snapshot().orders[0].remaining_quantity == D('8')
    assert early.snapshot().capital.total == D('10')
    ex.now = request().expires_at+timedelta(seconds=120)
    ledger = Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    state = ledger.snapshot()
    assert state.cash == D('99.20')
    assert state.positions[0].quantity == D('2') and state.positions[0].cost_usdc == D('.80')
    assert state.orders[0].filled_quantity == D('2') and state.orders[0].remaining_quantity == 0
    assert state.orders[0].authoritative and state.orders[0].status == 'cancelled'
    assert state.capital.total == D('2') and not state.pending
    ledger.reconcile(live)
    assert ledger.snapshot() == state
    journal.close()
    live,journal = adapter(tmp_path,ex.sdk)
    ledger = Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    assert ledger.snapshot() == state and len(journal.events('fill')) == 1
    before = ex.sdk.post_order.call_count
    live.place_order(request(),at=AT)
    assert ex.sdk.post_order.call_count == before


@pytest.mark.parametrize('state,quantity', [('live','0'),('cancelled','2'),('expired','0'),('filled','10')])
def test_missing_history_uses_open_orders_and_trades(tmp_path,state,quantity):
    from types import SimpleNamespace
    from polymarket.errors import RequestRejectedError
    from tests.fakes.live_exchange import Exchange
    from lp_v2.ledger import Ledger
    ex = Exchange()
    live,journal = adapter(tmp_path,ex.sdk)
    oid = live.place_order(request(),at=AT).order.order_id
    ex.reject_tokens.add('102')
    live.place_order(replace(request(),request_id='opposite',quote=Quote(CID,'102',D('.5'),D('10'))),at=AT)
    if D(quantity):
        ex.fill(oid,quantity,'.40')
    if state == 'cancelled':
        live.cancel_order(oid,at=ex.now)
    elif state != 'live':
        ex.terminal(oid)
    if state == 'expired':
        live.clock = lambda: request().expires_at+timedelta(seconds=120)
    foreign = ex.orders[oid].model_copy(update={'id':'foreign','maker_address':SIGNER,'status':'LIVE'})
    ex.orders['foreign'] = foreign
    ex.sdk.get_order.side_effect = RequestRejectedError('synthetic secret',status=404)
    ex.sdk.list_open_orders.side_effect = lambda **kw: iter((SimpleNamespace(items=(foreign,)),
        SimpleNamespace(items=(ex.orders[oid],) if state == 'live' else ())))
    fact = live.read_orders()[0]
    assert fact.authoritative and fact.order_id == oid
    assert fact.status == ('live' if state == 'live' else 'filled' if state == 'filled' else 'cancelled')
    assert fact.filled_quantity == D(quantity)
    assert fact.remaining_quantity == (D('10') if state == 'live' else 0)
    ledger = Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    snapshot = ledger.snapshot()
    assert snapshot.cash == D('100')-D(quantity)*D('.40')
    assert sum((p.quantity for p in snapshot.positions),D('0')) == D(quantity)
    assert snapshot.capital.inventory_cost == D(quantity)*D('.40')
    ledger.reconcile(live)
    assert ledger.snapshot() == snapshot
    journal.close()
    live,journal = adapter(tmp_path,ex.sdk)
    ledger = Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    assert ledger.snapshot() == snapshot
    with pytest.raises(ValueError):
        live.cancel_order('foreign',at=AT)
    assert ex.sdk.cancel_order.call_count == (1 if state == 'cancelled' else 0) and len(ex.posts) == 2
    assert all(c.kwargs['order_id'] != 'foreign' for c in ex.sdk.cancel_order.call_args_list)
    assert 'synthetic secret' not in str(journal.events())


@pytest.mark.parametrize('mode',['transport','5xx','open-page','trade-page','identity','unsettled','lag','blank-id'])
def test_missing_history_preserves_uncertainty_on_incomplete_evidence(tmp_path,mode):
    from types import SimpleNamespace
    from polymarket.errors import RequestRejectedError
    from tests.fakes.live_exchange import Exchange
    from lp_v2.ledger import Ledger
    ex = Exchange()
    live,journal = adapter(tmp_path,ex.sdk)
    live.clock = lambda: request().expires_at+timedelta(seconds=120)
    oid = live.place_order(request(),at=AT).order.order_id
    if mode == 'blank-id':
        ex.reject_tokens.add('102')
        live.place_order(replace(request(),request_id='opposite',quote=Quote(CID,'102',D('.5'),D('10'))),at=AT)
    ex.fill(oid,'2','.4')
    prior = live.read_orders()[0]
    assert prior.filled_quantity == D('2') and prior.authoritative
    ledger = Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    ex.terminal(oid)
    ex.sdk.get_order.side_effect = (TimeoutError('synthetic secret') if mode == 'transport'
        else RequestRejectedError('synthetic secret',status=500 if mode == '5xx' else 404))
    def partial():
        yield SimpleNamespace(items=())
        raise TimeoutError('synthetic secret')
    if mode == 'open-page':
        ex.sdk.list_open_orders.side_effect = lambda **kw: partial()
    elif mode == 'trade-page':
        ex.sdk.list_account_trades.side_effect = lambda **kw: partial()
    elif mode == 'identity':
        ex.sdk.list_open_orders.side_effect = lambda **kw: (SimpleNamespace(items=(ex.orders[oid].model_copy(update={'maker_address':SIGNER,'status':'LIVE'}),)),)
    elif mode == 'blank-id':
        from polymarket.models.clob.account import OpenOrder
        ex.orders[oid] = ex.orders[oid].model_copy(update={'status':'LIVE'})
        malformed = OpenOrder.model_validate(dict(ex.orders[oid].model_dump(by_alias=True),id=''))
        ex.sdk.list_open_orders.side_effect = lambda **kw: (SimpleNamespace(items=()),SimpleNamespace(items=(malformed,)))
    elif mode == 'unsettled':
        ex.trades[0] = ex.trades[0].model_copy(update={'status':'MINED'})
    elif mode == 'lag':
        raw = ex.trades[0]
        ex.trades[0] = raw.model_copy(update={'maker_orders':(raw.maker_orders[0].model_copy(update={'matched_amount':D('1')}),)})
    fact = live.read_orders()[0]
    assert not fact.authoritative and fact.filled_quantity == D('2') and fact.remaining_quantity == D('8')
    assert not live.orders_verified() and live.known_open_order_ids() == (oid,)
    if mode == 'blank-id':
        assert ex.orders[oid].status == 'LIVE'
    try:
        ledger.reconcile(live)
    except (TimeoutError,ValueError):
        pass
    assert ledger.snapshot().capital.total == D('10')
    assert not any(r.order is None and r.outcome == Outcome.FAILED for _,r in journal.events('order_result')
                   if r.request.request_id == request().request_id)
    assert 'synthetic secret' not in str(journal.events())


@pytest.mark.parametrize('mode',['success','applied-timeout','refused'])
def test_cancel_invalidates_current_cycle_readback(tmp_path,mode):
    from collections import Counter
    from tests.fakes.live_exchange import Exchange
    from polymarket.models.clob.cancel import CancelOrdersResponse
    from lp_v2.ledger import Ledger
    ex = Exchange()
    live,journal = adapter(tmp_path,ex.sdk)
    oid = live.place_order(request(),at=AT).order.order_id
    other = live.place_order(replace(request(),request_id='opposite',quote=Quote(CID,'102',D('.5'),D('10'))),at=AT).order.order_id
    if mode == 'applied-timeout':
        def cancel(**kw):
            ex.cancel(**kw)
            raise TimeoutError('synthetic secret')
        ex.sdk.cancel_order.side_effect = cancel
    elif mode == 'refused':
        ex.sdk.cancel_order.side_effect = lambda **kw: CancelOrdersResponse(canceled=(),not_canceled={oid:'refused'})
    with live.read_cycle(now=AT):
        assert all(o.authoritative and o.status == 'live' for o in live.read_orders())
        ex.fill(oid,'2','.40')
        live.cancel_order(oid,at=AT+timedelta(seconds=1))
        count = len(journal.events('order_result'))
        facts = {o.order_id:o for o in live.read_orders()}
        actual = facts[oid]
        assert actual.authoritative and actual.filled_quantity == D('2')
        assert actual.status == ('live' if mode == 'refused' else 'cancelled')
        assert actual.remaining_quantity == (D('8') if mode == 'refused' else 0)
        if mode != 'refused':
            assert all(not r.order or not r.order.authoritative or r.order.status != 'live'
                       for _,r in journal.events('order_result')[count:] if r.request.request_id == 'req-1')
        counts = Counter(c.kwargs['order_id'] for c in ex.sdk.get_order.call_args_list)
        assert counts[oid] == 2 and counts[other] == 1
    ledger = Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    state = ledger.snapshot()
    ledger.reconcile(live)
    assert ledger.snapshot() == state and state.positions[0].quantity == D('2')
    assert len(state.fills) == 1 and 'synthetic secret' not in str(journal.events())


@pytest.mark.parametrize('quantity',['0','2'])
def test_missing_unexpired_order_keeps_reservation(tmp_path,quantity):
    from types import SimpleNamespace
    from polymarket.errors import RequestRejectedError
    from tests.fakes.live_exchange import Exchange
    from lp_v2.ledger import Ledger
    ex = Exchange()
    live,journal = adapter(tmp_path,ex.sdk)
    oid = live.place_order(request(),at=AT).order.order_id
    ex.reject_tokens.add('102')
    live.place_order(replace(request(),request_id='opposite',quote=Quote(CID,'102',D('.5'),D('10'))),at=AT)
    if D(quantity):
        ex.fill(oid,quantity,'.4')
    ledger = Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    get = ex.sdk.get_order.side_effect
    opens = ex.sdk.list_open_orders.side_effect
    ex.sdk.get_order.side_effect = RequestRejectedError('synthetic-secret-never-log',status=404)
    ex.sdk.list_open_orders.side_effect = lambda **kw: (SimpleNamespace(items=()),)
    with live.read_cycle(now=AT+timedelta(seconds=10)):
        fact = live.read_orders()[0]
        assert not fact.authoritative and fact.status == 'live'
        assert fact.filled_quantity == D(quantity) and fact.remaining_quantity == D('10')-D(quantity)
        assert not live.orders_verified() and live.known_open_order_ids() == (oid,)
        ledger.reconcile(live)
        assert ledger.snapshot().capital.total == D('10')
        assert ledger.snapshot().capital.inventory_cost == D(quantity)*D('.4')
    assert ex.orders[oid].status == 'LIVE'
    assert not any(r.order and r.order.authoritative and r.order.status != 'live'
                   for _,r in journal.events('order_result'))
    count = ex.sdk.post_order.call_count
    assert live.place_order(request(),at=AT).outcome == Outcome.UNKNOWN
    assert ex.sdk.post_order.call_count == count
    ex.sdk.get_order.side_effect,ex.sdk.list_open_orders.side_effect = get,opens
    with live.read_cycle(now=AT+timedelta(seconds=20)):
        fact = live.read_orders()[0]
        assert fact.authoritative and fact.order_id == oid and fact.request == request()
        assert fact.status == 'live' and fact.filled_quantity == D(quantity)
        ledger.reconcile(live)
    assert ledger.snapshot().capital.total == D('10')
    assert len(ledger.snapshot().fills) == (1 if D(quantity) else 0)
    assert 'synthetic-secret-never-log' not in str(journal.events())


@pytest.mark.parametrize('reason,quantity', [('cancel','2'),('before-buffer','2'),('expired','2'),('full','10')])
def test_missing_history_terminal_requires_reason(tmp_path,reason,quantity):
    from polymarket.errors import RequestRejectedError
    from tests.fakes.live_exchange import Exchange
    from lp_v2.ledger import Ledger
    ex = Exchange()
    live,journal = adapter(tmp_path,ex.sdk)
    live.clock = lambda: ex.now
    oid = live.place_order(request(),at=AT).order.order_id
    ex.reject_tokens.add('102')
    live.place_order(replace(request(),request_id='opposite',quote=Quote(CID,'102',D('.5'),D('10'))),at=AT)
    ex.fill(oid,quantity,'.4')
    if quantity != '10':
        live.read_orders()
    if reason == 'cancel':
        live.cancel_order(oid,at=ex.now)
    else:
        ex.terminal(oid)
    if reason in ('before-buffer','expired'):
        ex.now = request().expires_at+timedelta(seconds=119 if reason == 'before-buffer' else 120)
    ex.sdk.get_order.side_effect = RequestRejectedError('synthetic-secret-never-log',status=404)
    fact = live.read_orders()[0]
    assert fact.order_id == oid and fact.request == request() and fact.filled_quantity == D(quantity)
    assert fact.authoritative is (reason != 'before-buffer')
    assert fact.status == ('live' if reason == 'before-buffer' else 'filled' if reason == 'full' else 'cancelled')
    assert fact.remaining_quantity == (D('8') if reason == 'before-buffer' else 0)
    ledger = Ledger(OWNER,D('100'))
    ledger.reconcile(live)
    state = ledger.snapshot()
    assert state.cash == D('100')-D(quantity)*D('.4')
    assert state.positions[0].quantity == D(quantity) and state.positions[0].cost_usdc == D(quantity)*D('.4')
    assert state.capital.total == (D('10') if reason == 'before-buffer' else D(quantity))
    assert len(state.fills) == 1 and len(ex.posts) == 2
    assert not any(r.outcome == Outcome.FAILED for _,r in journal.events('order_result') if r.request == request())
    assert 'synthetic-secret-never-log' not in str(journal.events())


def raw_order_context(sdk):
    """Wire bridge for the existing external SDK fake and its read assertions."""
    from types import SimpleNamespace
    def get_json(path, *, params):
        raw = sdk.get_order(order_id=path.removeprefix('/data/order/'))
        if raw is None:
            return None
        wire = raw.model_dump(mode='json')
        wire['asset_id'] = wire.pop('token_id')
        wire['market'] = wire.pop('condition_id')
        wire.pop('expires_at')
        wire['expiration'] = int(raw.expires_at.timestamp()) if raw.expires_at is not None else 0
        return wire
    import httpx
    import json
    transport = SimpleNamespace(get_json=get_json)
    transport._request = lambda method, path, **kw: httpx.Response(200, content=json.dumps(transport.get_json(path, **kw)).encode())
    object.__setattr__(sdk, '_ctx', SimpleNamespace(secure_clob=transport))


@pytest.mark.parametrize('proof', ['cancel', 'expired', 'neither'])
def test_null_readback_closes_only_with_existing_missing_order_proof(tmp_path, proof):
    from tests.fakes.live_exchange import Exchange
    from lp_v2.models import OrderStatus
    ex = Exchange()
    live, journal = adapter(tmp_path, ex.sdk)
    accepted = live.place_order(request(), at=AT)
    oid = accepted.order.order_id
    if proof == 'cancel':
        live.cancel_order(oid, at=AT)
    ex.orders.clear()
    raw_order_context(ex.sdk)
    ex.sdk._ctx.secure_clob.get_json = lambda *a, **kw: None
    ex.sdk.get_order.side_effect = ValueError('typed parser rejects null')
    now = request().expires_at + timedelta(seconds=120) if proof == 'expired' else AT
    with live.read_cycle(now=now):
        order = live.read_orders()[0]
        assert order.authoritative is (proof != 'neither')
        assert live.orders_verified() is (proof != 'neither')
        assert order.status == (OrderStatus.CANCELLED if proof != 'neither' else OrderStatus.LIVE)
    assert len(ex.posts) == 1
    journal.close()


def test_null_readback_open_order_is_still_cancelled(tmp_path):
    from tests.fakes.live_exchange import Exchange
    ex = Exchange()
    live, journal = adapter(tmp_path, ex.sdk)
    oid = live.place_order(request(), at=AT).order.order_id
    ex.sdk._ctx.secure_clob.get_json = lambda *a, **kw: None
    order = live.read_orders()[0]
    assert order.status.value == 'live'
    live.cancel_order(oid, at=AT)
    assert ex.sdk.cancel_order.call_count == 1
    assert ex.orders[oid].status == 'CANCELED'
    journal.close()


@pytest.mark.parametrize('status, body, closed', [
    (200, b'null\n', True), (404, b'{}', True), (500, b'null', False),
    (201, b'null', False), (202, b'null', False), (204, b'', False),
    (200, b'{}', False), (200, b'false', False), (200, b'[]', False),
])
def test_order_readback_real_http_transport(tmp_path, status, body, closed):
    import httpx
    from polymarket.clients._transport import SyncTransport
    from tests.fakes.live_exchange import Exchange
    ex = Exchange()
    live, journal = adapter(tmp_path, ex.sdk)
    oid = live.place_order(request(), at=AT).order.order_id
    live.cancel_order(oid, at=AT)
    ex.orders.clear()
    seen = []
    def respond(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(status, content=body)
    with httpx.Client(base_url='https://clob.example', transport=httpx.MockTransport(respond)) as http:
        ex.sdk._ctx.secure_clob = SyncTransport(base_url='https://clob.example', client=http)
        ex.sdk.get_order.side_effect = lambda **kw: SecureClient.get_order(ex.sdk, **kw)
        with live.read_cycle(now=AT):
            fact = live.read_orders()[0]
            assert fact.authoritative is closed
            assert live.orders_verified() is closed
    assert seen == [('GET', '/data/order/' + oid)]
    assert len(ex.posts) == 1
    journal.close()
