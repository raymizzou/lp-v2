"""Approved PR-B public-seam regressions; external boundaries only are faked."""
from datetime import timedelta
from decimal import Decimal as D
import httpx
from lp_v2 import journal, live_runtime, live_sdk, rotation_live, rotation_state
from tests.test_live_sdk import AT, CID, TOKENS
from tests.test_rotation_live import rotation_world, BID, CID_C


def inventory_adapter():
    from eth_account import Account
    from polymarket.environments import PRODUCTION
    from polymarket._internal.wallet import derive_proxy_wallet_address
    from lp_v2.readonly_sdk import L2Credentials
    from tests.test_readonly_sdk import clocked_sdk, external_sdk, market_wire
    signer=Account.from_key(bytes([7])*32).address
    creds=L2Credentials('sentinel-key','c2VjcmV0','sentinel-pass',signer,derive_proxy_wallet_address(signer,PRODUCTION.wallet_derivation),1)
    public,_=external_sdk([market_wire(cid=CID)])
    def handle(r):
        assert r.method=='GET'
        return httpx.Response(200,json=dict(balance='1234567' if r.url.params['token_id']=='101' else '0',allowances={}))
    return clocked_sdk(public=public,http=httpx.Client(transport=httpx.MockTransport(handle)),credentials=creds)


def test_balance_evidence_reports_source_time_and_unknowns(tmp_path):
    sdk=inventory_adapter()
    data=sdk.read_conditional_inventory(AT,CID)
    assert data['quantities']=={'101':D('1.234567'),'102':D('0')}
    assert data['source']=='authenticated GET /balance-allowance CONDITIONAL'
    assert data['observed_at']==AT.isoformat()
    assert data['upstream_unit']==data['upstream_precision']==data['exchange_as_of']=='UNKNOWN'
    assert data['adapter_divisor']=='1000000'
    sdk.test_clock[0]=AT+timedelta(seconds=5)
    later=sdk.read_conditional_inventory(AT,CID)
    assert later['observed_at']==(AT+timedelta(seconds=5)).isoformat()
    assert later['exchange_as_of']=='UNKNOWN'
    rotation,ex,obs,clock=rotation_world(tmp_path)
    inventory_reads=[]
    def inventory(now,cid):
        inventory_reads.append(cid)
        return dict(data,as_of=now.isoformat(),observed_at=now.isoformat())
    obs.read_conditional_inventory=inventory
    rotation.step(AT)
    clock[0]+=timedelta(seconds=10);rotation.scanner.scan_once(clock[0])
    report=rotation.step(clock[0])['rotation']
    assert report['phase']=='running' and report['current']==BID
    assert report['blocker']=='UNKNOWN' and inventory_reads==[]
    assert 'sentinel' not in str(report)
    sdk.close();rotation.close()


def expired_world(tmp_path):
    from tests.fakes.live_exchange import Exchange
    from tests.test_live_sdk import adapter, request
    ex=Exchange();sdk,journal=adapter(tmp_path,ex.sdk)
    sdk.place_order(request(),at=AT)
    ex.orders.clear()
    ex.sdk.list_account_trades.side_effect=TimeoutError('sentinel-secret')
    return sdk,ex,journal,AT+timedelta(hours=1,seconds=120)


def test_expired_absence_proof_survives_trade_read_failure(tmp_path):
    sdk,ex,journal,at=expired_world(tmp_path)
    original=journal.events('fill')
    with sdk.read_cycle(now=at):sdk.read_orders()
    assert sdk.known_open_order_ids()==('order-1',)
    with sdk.read_cycle(now=at+timedelta(seconds=10)):sdk.read_orders()
    assert sdk.known_open_order_ids()==()
    with sdk.read_cycle(now=at+timedelta(seconds=20)):sdk.read_orders()
    assert sdk.known_open_order_ids()==()
    assert journal.events('fill')==original and len(ex.posts)==1
    with sdk.read_cycle(now=at+timedelta(seconds=30)):
        assert sdk.read_orders()[0].filled_quantity==D('0')
    assert not sdk.orders_verified()  # absence is no fill/accounting proof
    journal.close()
    # A timed-out acceptance retains both signed candidates and financial UNKNOWN.
    from tests.fakes.live_exchange import Exchange
    from tests.test_live_sdk import adapter, request
    path=tmp_path/'pending';path.mkdir()
    ex=Exchange();sdk,journal=adapter(path,ex.sdk)
    ex.sdk.post_order.side_effect=TimeoutError('sentinel-secret')
    sdk.place_order(request(),at=AT)
    ex.sdk.list_account_trades.side_effect=TimeoutError('sentinel-secret')
    with sdk.read_cycle(now=at):sdk.read_orders()
    assert not sdk.execution_risk_verified()
    with sdk.read_cycle(now=at+timedelta(seconds=10)):sdk.read_orders()
    assert sdk.execution_risk_verified() and not sdk.orders_verified()
    assert len(journal.events('signed'))==1 and ex.sdk.post_order.call_count==1
    journal.close()


def test_absence_proof_resets_after_incomplete_or_present_list(tmp_path):
    from types import SimpleNamespace
    from dataclasses import replace
    from tests.test_live_sdk import request
    for mode in ('incomplete','present'):
        path=tmp_path/mode;path.mkdir()
        sdk,ex,journal,at=expired_world(path)
        with sdk.read_cycle(now=at):
            sdk.read_orders();sdk.read_orders()
        assert sdk.known_open_order_ids()==('order-1',)
        def pages(**kw):
            yield SimpleNamespace(items=())
            if mode=='incomplete':raise TimeoutError('sentinel-secret')
            # Authentic current presence must override any older absence.
            from polymarket.models.clob.account import OpenOrder
            yield SimpleNamespace(items=(OpenOrder(id='order-1',market=CID,asset_id='101',owner='api-owner',
                maker_address=sdk.wallet,side='BUY',price='.4',original_size='10',size_matched='0',
                outcome='Yes',order_type='GTD',status='LIVE',created_at=AT,expiration=int(request().expires_at.timestamp())),))
        ex.sdk.list_open_orders.side_effect=pages
        with sdk.read_cycle(now=at+timedelta(seconds=10)):sdk.read_orders()
        ex.sdk.list_open_orders.side_effect=lambda **kw:(SimpleNamespace(items=()),)
        with sdk.read_cycle(now=at+timedelta(seconds=20)):sdk.read_orders();sdk.read_orders()
        assert sdk.known_open_order_ids()==('order-1',)
        with sdk.read_cycle(now=at+timedelta(seconds=30)):sdk.read_orders()
        assert sdk.known_open_order_ids()==()
        ex.sdk.list_open_orders.side_effect=pages
        with sdk.read_cycle(now=at+timedelta(seconds=40)):sdk.read_orders()
        assert sdk.known_open_order_ids()==('order-1',)
        assert not sdk.orders_verified() and journal.events('fill')==()
        journal.close()


def test_zero_exchange_balance_keeps_unreconciled_ledger_blocked(tmp_path):
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(AT)
    ex.fill('order-1','1','.40',at=AT+timedelta(seconds=1))
    clock[0]+=timedelta(seconds=10)
    # First ingest the actual fill with chain balance matching, without opening.
    rotation.current.step(clock[0],allow_open=False)
    old=rotation.current
    before=old.ledger.snapshot().positions
    assert before[0].quantity==D('1') and before[0].cost_usdc==D('.40')
    ex.quantities['101']=D('0')
    obs.fail_account=True
    rotation.scanner.scan_once(clock[0])
    clock[0]+=timedelta(seconds=10)
    report=rotation.step(clock[0])['rotation']
    assert report['phase']=='cleared' and report['current'] is None and report['entered_count']==0
    assert old.ledger.snapshot().positions==before
    assert old.sdk.journal.events('exit_observation') and old.stop.is_set()
    assert report['blocker']['code']=='no_revalidated_candidate'
    assert report['blocker']['numbers']=='UNKNOWN'
    assert report['exit_observation']['source']=='open_orders'
    assert report['exit_observation']['complete'] is True and report['exit_observation']['owned_open_count']==0
    assert old.sdk.journal.events('fill')[-1][1].quantity==1
    assert {p.token_id for p in ex.posts}<=set(TOKENS)
    rotation.close()


def test_nonzero_inventory_blocker_keeps_complete_evidence(tmp_path):
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(AT)
    # Dust stays outside program handling; no balance read or automatic transfer.
    ex.quantities['101']=D('.008133')
    clock[0]+=timedelta(seconds=10);rotation.scanner.scan_once(clock[0])
    first=clock[0]
    report=rotation.step(clock[0])['rotation']
    for elapsed in (0,3599,3600):
        if elapsed:
            clock[0]=first+timedelta(seconds=elapsed);report=rotation.step(clock[0])['rotation']
        assert report['phase']=='running' and report['current']==BID and report['entered_count']==1
        assert ex.quantities['101']==D('.008133')
        if isinstance(report['blocker'],dict):assert report['blocker']['code']=='window_expired'
        assert sum(p.token_id in TOKENS for p in ex.posts)==2
        assert all(c.kwargs['asset_type']!='CONDITIONAL' for c in ex.sdk.get_balance_allowance.call_args_list)
    ex.quantities['101']=D('0');clock[0]+=timedelta(seconds=10)
    report=rotation.step(clock[0])['rotation']
    assert report['phase']=='running' and report['current']==BID and report['entered_count']==1
    assert sum(p.token_id in TOKENS for p in ex.posts)==2
    rotation.close()


def test_runtime_self_proof_allows_entry_without_external_startup_record(tmp_path,monkeypatch):
    import json
    from pathlib import Path
    from tests.test_rotation_live import factory_boundary
    from lp_v2.live_runtime import create_live_runner
    config,ex,clock,record,seen=factory_boundary(tmp_path,monkeypatch)
    runner=create_live_runner(config,clock=lambda:clock[0])
    runner.scanner.scan_once(clock[0]);runner.step(clock[0])
    assert runner.condition_id==CID and tuple(s.token_id for s in ex.posts)==TOKENS
    runner.step(clock[0])
    assert runner.condition_id==CID and tuple(s.token_id for s in ex.posts)==TOKENS
    clock[0]+=timedelta(seconds=10);report=runner.step(clock[0])['rotation']
    assert report['current']==BID and report['entered_count']==1
    assert tuple(s.token_id for s in ex.posts[:2])==TOKENS
    assert {s.token_id for s in ex.posts[2:]}=={'201','202'}
    assert 'startup_verified' not in json.loads((Path(config.state_dir)/'live-control-run.json').read_text())
    runner.close()


def test_self_proof_recovers_after_old_accounting_failure(tmp_path,monkeypatch):
    from tests.test_rotation_live import factory_boundary
    from lp_v2.live_runtime import create_live_runner
    config,ex,clock,record,seen=factory_boundary(tmp_path,monkeypatch)
    transport=httpx.HTTPTransport.handle_request
    def handle(self,r):
        if r.url.path=='/balance-allowance' and r.url.params.get('asset_type')=='CONDITIONAL':
            return httpx.Response(200,json=dict(balance=str(int(ex.quantities.get(r.url.params['token_id'],D(0))*1000000)),allowances={}),request=r)
        return transport(self,r)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',handle)
    runner=create_live_runner(config,clock=lambda:clock[0])
    runner.step(clock[0]);clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert {s.token_id for s in ex.posts}==set(TOKENS)
    ex.fill('order-1','1','.40',at=clock[0]+timedelta(seconds=1))
    # Match this factory's derived wallet in the external fake trade response.
    trade=ex.trades[-1];wallet=runner.sdk.wallet
    ex.trades[-1]=trade.model_copy(update=dict(maker_address=wallet,
        maker_orders=[m.model_copy(update=dict(maker_address=wallet)) for m in trade.maker_orders]))
    clock[0]+=timedelta(seconds=10);runner.current.step(clock[0],allow_open=False)
    old=runner.current;before=old.ledger.snapshot().positions
    ex.quantities['101']=D(0)
    clock[0]+=timedelta(seconds=10);runner.scanner.scan_once(clock[0]);report=runner.step(clock[0])['rotation']
    assert report['phase']=='running' and report['current']==BID and report['entered_count']==1
    assert report['blocker']=='UNKNOWN'
    assert tuple(s.token_id for s in ex.posts)==('101','102','201','202')
    runner.step(clock[0]);assert runner.entered_count==1 and len(ex.posts)==4
    ex.quantities['101']=D(1)
    for _ in range(2):
        clock[0]+=timedelta(seconds=10);report=runner.step(clock[0])['rotation']
        assert report['current']==BID and report['entered_count']==1
        assert old.ledger.snapshot().positions==before and old.stop.is_set()
    assert old.sdk.journal.events('fill')[-1][1].quantity==1
    assert tuple(s.token_id for s in ex.posts)==('101','102','201','202')
    runner.close()
    # Actual read failure can recover when both financial sources really agree.
    # Clean candidate retry evidence now lives in the legal cleared-restart case.
    recovery=tmp_path/'read-recovery';recovery.mkdir()
    config,ex,clock,record,seen=factory_boundary(recovery,monkeypatch)
    read_clock=shared_runtime_clock(monkeypatch,clock,'advancing')
    runner=create_live_runner(config,clock=read_clock)
    runner.scanner.scan_once(clock[0]);runner.step(clock[0])
    ex.sdk.get_balance_allowance.side_effect=TimeoutError('temporary balance unavailable')
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.entered_count==0 and {p.token_id for p in ex.posts}<=set(TOKENS)
    ex.sdk.get_balance_allowance.side_effect=ex.balance
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.entered_count==0 and {p.token_id for p in ex.posts}<=set(TOKENS)
    runner.step(clock[0]);assert runner.entered_count==0
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID and runner.entered_count==1
    assert sum(p.token_id in ('201','202') for p in ex.posts)==2
    signatures=tuple(p['request_id'] for _,p in runner.sdk.journal.events('signed'))
    assert len(signatures)==len(set(signatures))==2
    runner.close()


def test_target_foreign_orders_are_scoped_and_report_tails():
    from lp_v2.readonly_sdk import L2Credentials
    from tests.test_readonly_sdk import clocked_sdk
    rows=[dict(id='owned-target-12345678',market=CID),dict(id='foreign-other-12345678',market=BID),dict(id='foreign-target-87654321',market=CID)]
    seen=[];partial=[False]
    def handle(r):
        seen.append(r.method)
        if r.url.params.get('next_cursor'):return httpx.Response(503,json={})
        return httpx.Response(200,json=dict(data=rows,next_cursor='next' if partial[0] else 'LTE='))
    sdk=clocked_sdk(http=httpx.Client(transport=httpx.MockTransport(handle)),credentials=L2Credentials('sentinel','c2VjcmV0','sentinel','0x'+'a'*40,'0x'+'b'*40,1))
    own=('owned-target-12345678',)
    report=sdk.read_orders(AT,condition_id=CID,known_order_ids=own)
    assert report['legacy_order_count']==1 and report['non_target_order_count']==1
    assert report['foreign_order_tails']==['87654321']
    assert 'foreign-target-' not in str(report)
    rows.pop();report=sdk.read_orders(AT,condition_id=CID,known_order_ids=own)
    assert report['legacy_order_count']==0 and report['non_target_order_count']==1
    assert sdk.read_orders(AT)['legacy_order_count']==2
    partial[0]=True
    assert sdk.read_orders(AT,condition_id=CID,known_order_ids=own)['legacy_order_count']=='UNKNOWN'
    partial[0]=False;rows.append(dict(id='unclassified'))
    assert sdk.read_orders(AT,condition_id=CID,known_order_ids=own)['legacy_order_count']=='UNKNOWN'
    assert set(seen)=={'GET'}
    sdk.close()


from tests.test_live_control_host import combined_world, target, operator_os, seal_fixture_journals
from tests.test_live_control_profiles import profile, config_home


def stopped_history(world):
    import copy,json
    from pathlib import Path
    from lp_v2.journal import Journal
    from lp_v2.models import Position
    state=Path(world.profile['files']['state_dir']);p=world.profile
    started=world.clock-timedelta(seconds=30);at=world.clock-timedelta(seconds=10)
    owner='lp-v2:'+p['identity']['wallet_address'].lower();market=p['trading']['market_condition_id']
    for name,data in [('live-control-run.json',dict(profile=copy.deepcopy(p),run_id='f'*32,started_at=started.isoformat(),journal_seq=1)),
        ('process.json',dict(run_id='f'*32,pid=45678,identity='synthetic-boot:123')),('stop-result.json',dict(run_id='f'*32,exit_code=1))]:
        path=state/name;path.write_text(json.dumps(data));path.chmod(0o600)
    journal=Journal(state/'journal.sqlite')
    journal.append('identity',dict(owner=owner,condition_id=market,token_ids=('101','102')))
    journal.append('occupation',dict(at=started))
    journal.append('stop_result',dict(at=at,exit_code=1,orders=[],pending=[],positions=[Position(owner,market,'101',D('1.25'),D('.5'),at)],
        merges=[],inventory=[],reconciliation='UNKNOWN',accounting_as_of=at))
    journal.close();(state/'journal.sqlite').chmod(0o600)
    seal_fixture_journals(state)
    return state


def test_clean_live_handoff_ignores_previous_exit_one(combined_world,capsys):
    from lp_v2.cli import main
    world=combined_world;state=stopped_history(world)
    seal_fixture_journals(state)
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert main(['live','check'])==0
    import json
    report=json.loads(capsys.readouterr().out)
    assert 'inventory_handoff' not in report['checks']
    assert frozen=={p:p.read_bytes() for p in frozen}
    assert world.mutations==[]
    # Failed accounting can leave the final ledger's order snapshot stale even
    # though this stopped executor can no longer submit and fresh opens are empty.
    from dataclasses import replace
    from lp_v2.journal import Journal
    from lp_v2.models import Order,OrderResult,OrderStatus,Outcome
    from tests.test_live_sdk import request
    journal=Journal(state/'journal.sqlite');drain=journal.events('stop_result')[-1][1]
    req=request();req=replace(req,owner='lp-v2:'+world.profile['identity']['wallet_address'].lower(),
        quote=replace(req.quote,condition_id=world.profile['trading']['market_condition_id']))
    order=Order('previous-order',req,D(0),D(10),OrderStatus.LIVE,drain['at'],False)
    journal.append('order_result',OrderResult(req,Outcome.UNKNOWN,order,drain['at']))
    journal.append('stop_result',dict(drain,orders=[order]));journal.close()
    seal_fixture_journals(state)
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert main(['live','check'])==0
    assert 'inventory_handoff' not in json.loads(capsys.readouterr().out)['checks']
    assert frozen=={p:p.read_bytes() for p in frozen} and world.mutations==[]


import pytest


@pytest.mark.parametrize('dirty',['balance','order','merge'])
def test_dirty_handoff_numbers_survive_cli_normalization(combined_world,capsys,dirty):
    import json
    from lp_v2.cli import main
    from lp_v2.journal import Journal
    from lp_v2.models import MergeRequest,MergeResult,Outcome
    world=combined_world;state=stopped_history(world)
    market=world.profile['trading']['market_condition_id'];owner='lp-v2:'+world.profile['identity']['wallet_address'].lower()
    journal=Journal(state/'journal.sqlite');drain=journal.events('stop_result')[-1][1]
    if dirty=='balance':world.conditional['101']='8133'
    if dirty=='order':
        journal.append('signed',dict(order_ids=('owned-secret-prefix-12345678',)))
        world.http_overrides['/data/orders']=(200,dict(data=[dict(id='owned-secret-prefix-12345678',market=market)],next_cursor='LTE='))
    if dirty=='merge':
        req=MergeRequest('merge-pending',owner,market,'101','102',D('1'),drain['at'])
        journal.append('merge_intent',dict(request=req))
        drain=dict(drain,merges=[MergeResult(req,Outcome.UNKNOWN,D(0),drain['at'])])
    journal.append('stop_result',drain);journal.close()
    seal_fixture_journals(state)
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert main(['live','check'])==0
    report=json.loads(capsys.readouterr().out)
    assert 'inventory_handoff' not in report['checks']
    assert report['checks']['legacy_orders']['status']=='PASS'
    assert drain['positions'][0].quantity==D('1.25') and drain['positions'][0].cost_usdc==D('.5')
    if dirty=='merge':
        assert drain['merges'][0].outcome==Outcome.UNKNOWN and drain['merges'][0].received_usdc==0
    assert not any(r.url.params.get('asset_type')=='CONDITIONAL' for r in world.http_requests)
    assert frozen=={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert 'SECRET' not in str(report) and 'owned-secret-prefix' not in str(report)
    assert world.mutations==[]


def test_all_candidate_failures_back_off_with_visible_reasons(tmp_path):
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.window_end=AT+timedelta(hours=1)
    rotation.step(AT)
    obs.ranked=(CID,BID,CID_C);obs.fail_public={CID_C}
    account=obs.read_account_readiness
    def readiness(now,**kwargs):
        result=account(now,**kwargs)
        if kwargs['condition_id']==BID:result['balance']=dict(status='FAIL')
        return result
    obs.read_account_readiness=readiness
    obs.plans[CID]=__import__('dataclasses').replace(obs.plans[CID],books=tuple(__import__('dataclasses').replace(b,observed_at=AT-timedelta(seconds=100)) for b in obs.plans[CID].books))
    read_fixed=obs.read_fixed_market
    def fixed(now,cid):
        if cid==CID and now>AT:raise ValueError('sentinel-secret')
        return read_fixed(now,cid)
    # The retained complete catalog is valid; candidate-specific fresh reads fail.
    rotation.scanner.scan_once(AT)
    obs.read_fixed_market=fixed
    for sec in range(10,132):
        clock[0]=AT+timedelta(seconds=sec)
        report=rotation.step(clock[0])['rotation']
        if sec==10:
            next_retry=report['next_retry_at'];reads=len([r for r in obs.reads if r[0] in ('account_check','public_check')])
            assert len(report['candidate_failures'])==3
        if sec<40:
            assert report['next_retry_at']==next_retry
            assert len([r for r in obs.reads if r[0] in ('account_check','public_check')])==reads
        assert report['phase']=='cleared' and report['entered_count']==0
    rows=rotation.journal.events('revalidation')
    for second in range(0,132):
        assert sum(0<=(r['at']-(AT+timedelta(seconds=second))).total_seconds()<60 for _,r in rows)<=2
    assert all(len(r['candidates'])==3 for _,r in rows)
    assert 'sentinel' not in str(report)
    clock[0]=AT+timedelta(seconds=1810);report=rotation.step(clock[0])['rotation']
    assert report['blocker']['escalated'] and report['blocker']['next_step']
    clock[0]=rotation.window_end;report=rotation.step(clock[0])['rotation']
    assert report['reason']=='no_revalidated_candidate' and len(ex.posts)==2
    assert report['next_retry_at'] > clock[0].isoformat()
    rotation.close()


def test_official_start_accepts_bound_self_proven_rotation(tmp_path,monkeypatch,config_home):
    import json,datetime as datetime_module
    from datetime import datetime as RealDateTime
    from pathlib import Path
    from tests.test_rotation_live import factory_boundary
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.live_control import workflow,host
    from lp_v2.live_control.profiles import atomic_json,config_root
    config,ex,clock,record,seen=factory_boundary(tmp_path,monkeypatch)
    record['profile']['name']='tokyo'
    atomic_json(Path(config.state_dir)/'live-control-run.json',record)
    class Clock(RealDateTime):
        @classmethod
        def now(cls,tz=None):return clock[0]
    clock[0]=Clock.fromisoformat(clock[0].isoformat())
    monkeypatch.setattr(datetime_module,'datetime',Clock)
    monkeypatch.setattr('time.sleep',lambda seconds:clock.__setitem__(0,clock[0]+timedelta(seconds=seconds)))
    runner=create_live_runner(config,clock=lambda:clock[0])
    runner.scanner.scan_once(clock[0]);runner.step(clock[0])
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID
    assert 'startup_verified' not in json.loads((Path(config.state_dir)/'live-control-run.json').read_text())
    (config_root()/'operations').mkdir(exist_ok=True)
    atomic_json(config_root()/'operations'/(record['action_id']+'.json'),dict(action='on',profile=record['profile']))
    calls=[]
    def transport(profile,action,**kwargs):
        calls.append(action)
        if action=='off':return dict(status='PASS',drain='PASS')
        assert action=='observe' and kwargs['run_id']==record['run_id']
        runner.step(clock[0])
        snapshot=dict(run_id=record['run_id'],executor_running=True,readonly_stopped=True,
            identity=profile['identity'],summary=host.safe_summary(runner.snapshot()))
        snapshot['startup_verification']=host.record_start_observation(dict(profile=profile,
            action_id=record['action_id'],run_id=record['run_id']),snapshot)
        return snapshot
    monkeypatch.setattr(workflow,'request',transport)
    clock[0]+=timedelta(seconds=10)
    result=workflow.verify_start(record['profile'],dict(status='ACCEPTED',run_id=record['run_id'],started_at=record['started_at']),action_id=record['action_id'])
    assert result['status']=='PASS' and result['verified_rounds']==2
    assert 'off' not in calls and runner.condition_id==BID
    saved=json.loads((Path(config.state_dir)/'live-control-run.json').read_text())
    assert saved['startup_verified']['action_id']==record['action_id']
    assert saved['startup_verified']['run_id']==record['run_id']
    assert len(saved['startup_verified']['moments'])==2
    assert saved['startup_verified']['moments'][0][0] < saved['startup_verified']['moments'][1][0]
    runner.close()


def shared_runtime_clock(monkeypatch,current,mode):
    import datetime as datetime_module
    from datetime import datetime as RealDateTime
    def read_clock():
        if mode=='advancing':current[0]+=timedelta(microseconds=1)
        return current[0]
    class Clock(RealDateTime):
        @classmethod
        def now(cls,tz=None):return read_clock()
    current[0]=Clock.fromisoformat(current[0].isoformat())
    monkeypatch.setattr(datetime_module,'datetime',Clock)
    return read_clock


@pytest.mark.parametrize('clock_mode,guard',[(mode,guard)
    for mode in ('frozen','advancing')
    for guard in ('action','run','process','window','failed-round','duplicate','restart')],
    ids=[f'{mode}-{guard}' for mode in ('frozen','advancing')
         for guard in ('action','run','process','window','failed-round','duplicate','restart')])
def test_self_proof_preserves_current_binding_and_consecutive_rounds(tmp_path,monkeypatch,clock_mode,guard):
    import json
    from pathlib import Path
    from tests.test_rotation_live import factory_boundary
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.live_control.profiles import atomic_json
    from lp_v2.readonly_sdk import FixedMarketIneligible
    config,ex,clock,record,seen=factory_boundary(tmp_path,monkeypatch)
    read_clock=shared_runtime_clock(monkeypatch,clock,clock_mode)
    state=Path(config.state_dir)
    original_process=json.loads((state/'process.json').read_text())
    runner=create_live_runner(config,clock=read_clock);runner.scanner.scan_once(clock[0]);runner.step(clock[0])
    assert runner.condition_id==CID and tuple(p.token_id for p in ex.posts)==TOKENS
    if guard=='duplicate':
        runner.step(clock[0]);runner.step(clock[0])
    elif guard=='restart':
        runner.close();runner=create_live_runner(config,clock=read_clock);runner.scanner.scan_once(clock[0])
        clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    else:
        if guard=='action':atomic_json(state/'live-control-run.json',dict(record,action_id='c'*32))
        if guard=='run':atomic_json(state/'process.json',dict(original_process,run_id='c'*32))
        if guard=='process':atomic_json(state/'process.json',dict(original_process,identity='different-physical-process'))
        if guard=='window':atomic_json(state/'live-control-run.json',dict(record,window_end=(clock[0]+timedelta(minutes=4)).isoformat()))
        if guard=='failed-round':ex.sdk.get_balance_allowance.side_effect=TimeoutError('sentinel-secret')
        clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.entered_count==0 and {p.token_id for p in ex.posts}<=set(TOKENS)
    if guard not in ('duplicate','restart'):
        atomic_json(state/'live-control-run.json',record);atomic_json(state/'process.json',original_process)
        ex.sdk.get_balance_allowance.side_effect=ex.balance
        clock[0]=AT+timedelta(seconds=50)
        runner.step(clock[0])
        assert runner.entered_count==0 and {p.token_id for p in ex.posts}<=set(TOKENS)
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID and runner.entered_count==1
    assert tuple(p.token_id for p in ex.posts[-2:])==('201','202')
    assert sum(p.token_id in ('201','202') for p in ex.posts)==2
    # Old window metadata does not expire admission; real candidate failure still blocks it.
    clock[0]=runner.window_end
    def excluded(now,cid):raise FixedMarketIneligible('confirmed closed')
    runner.observations.read_fixed_market=excluded
    signed=len(ex.posts);report=runner.step(clock[0])['rotation']
    assert report['reason']=='no_revalidated_candidate' and report['entered_count']==1 and len(ex.posts)==signed
    runner.close()


def test_advancing_clock_active_market_collects_new_round_proof(tmp_path,monkeypatch):
    import json
    from pathlib import Path
    from tests.test_rotation_live import factory_boundary
    from lp_v2.live_runtime import create_live_runner
    config,ex,clock,record,seen=factory_boundary(tmp_path,monkeypatch)
    read_clock=shared_runtime_clock(monkeypatch,clock,'advancing')
    runner=create_live_runner(config,clock=read_clock)
    runner.scanner.scan_once(clock[0]);runner.step(clock[0])
    assert runner.condition_id==CID and tuple(p.token_id for p in ex.posts)==TOKENS
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID and runner.entered_count==1
    signed=tuple(p['request_id'] for _,p in runner.sdk.journal.events('signed'))
    assert len(signed)==len(set(signed))==2
    for _ in range(2):
        clock[0]+=timedelta(seconds=10);report=runner.step(clock[0])['rotation']
    assert report['reason']=='current_leads_or_ties'
    assert report['current']==BID and report['entered_count']==1 and report['switch_count']==1
    assert tuple(p['request_id'] for _,p in runner.sdk.journal.events('signed'))==signed
    assert tuple(p.token_id for p in ex.posts)==('101','102','201','202')
    assert 'startup_verified' not in json.loads((Path(config.state_dir)/'live-control-run.json').read_text())
    runner.close()


@pytest.mark.parametrize('variant',['clean','unexpired','partial','present','missing-signature','unknown-merge'])
def test_handoff_retains_unknown_accounting_after_proven_expired_absence(combined_world,monkeypatch,capsys,variant):
    import json
    from dataclasses import replace
    from pathlib import Path
    from tests.test_rotation_live import control_rotation_boundary
    from lp_v2.cli import main
    from lp_v2.journal import Journal
    from lp_v2.models import MergeRequest,MergeResult,Outcome
    from lp_v2.live_control.profiles import atomic_json
    world=combined_world;control_rotation_boundary(world,monkeypatch)
    state=Path(world.profile['files']['state_dir'])
    created=world.clock-timedelta(seconds=10 if variant=='unexpired' else 3730)
    runtime,ex,obs,clock=rotation_world(state,now=created,identity=world.profile['identity'],code_sha=world.profile['release']['expected_sha'])
    runtime.action_id='a'*32
    runtime.sdk.bind_entry(action_id=runtime.action_id,window_end=runtime.window_end,activation_id='pending-seed')
    end=runtime.window_end
    sign=ex.sign
    ex.sdk.create_limit_order.side_effect=lambda **kw:replace(sign(**kw),maker=runtime.sdk.wallet,signer=runtime.sdk.signer,timestamp=int(created.timestamp()*1000))
    ex.sdk.post_order.side_effect=TimeoutError('sentinel-secret')
    runtime.step(created)
    ex.sdk.list_account_trades.side_effect=TimeoutError('sentinel-secret')
    clock[0]=created+timedelta(seconds=10);runtime.scanner.scan_once(clock[0]);runtime.step(clock[0])
    clock[0]=world.clock;runtime.step(clock[0])
    clock[0]+=timedelta(seconds=10);runtime.step(clock[0]);world.clock=clock[0]
    current=runtime.current
    # Explicitly stop through the public lifecycle before validating handoff.
    current.drain(timeout_s=0)
    journal=current.sdk.journal;drain=journal.events('stop_result')[-1][1]
    assert len(drain['pending'])==2 and drain['reconciliation']=='UNKNOWN'
    ids=tuple(oid for _,p in journal.events('signed') for oid in p['order_ids'])
    if variant=='missing-signature':
        unsigned=replace(drain['pending'][0],request_id='unsigned-request')
        journal.append('intent',dict(request=unsigned))
        drain=dict(drain,pending=(*drain['pending'],unsigned));journal.append('stop_result',drain)
    if variant=='unknown-merge':
        req=MergeRequest('unknown-merge',runtime.sdk.owner,CID,'101','102',D(1),created)
        journal.append('merge_intent',dict(request=req))
        drain=dict(drain,merges=[MergeResult(req,Outcome.UNKNOWN,D(0),world.clock)])
        journal.append('stop_result',drain)
    runtime.close()
    (state/'markets'/CID/'journal.sqlite').chmod(0o600)
    record=dict(profile=world.profile,action_id='a'*32,run_id='f'*32,started_at=created.isoformat(),journal_seqs={CID:0},window_end=end.isoformat())
    atomic_json(state/'live-control-run.json',record)
    atomic_json(state/'process.json',dict(run_id='f'*32,pid=45678,identity='synthetic-stopped'))
    atomic_json(state/'stop-result.json',dict(run_id='f'*32,exit_code=drain['exit_code']))
    http=httpx.HTTPTransport.handle_request;observations=[]
    def pages(self,r):
        if r.url.path!='/data/orders':return http(self,r)
        observations.append(world.clock);world.clock+=timedelta(seconds=1)
        if variant=='partial' and r.url.params.get('next_cursor'):return httpx.Response(503,json={},request=r)
        return httpx.Response(200,json=dict(data=[dict(id=ids[0],market=CID)] if variant=='present' else [],next_cursor='next' if variant=='partial' else 'LTE='),request=r)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',pages)
    seal_fixture_journals(state)
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    code=main(['live','check']);result=json.loads(capsys.readouterr().out)
    assert 'inventory_handoff' not in result['checks']
    if variant=='partial':
        assert code==2 and result['checks']['legacy_orders']['status']=='UNKNOWN'
    else:
        assert code==0 and result['checks']['legacy_orders']['status']=='PASS'
    assert observations
    assert drain['reconciliation']=='UNKNOWN' and len(drain['pending'])==(3 if variant=='missing-signature' else 2)
    if variant=='unknown-merge':
        assert drain['merges'][0].outcome==Outcome.UNKNOWN and drain['merges'][0].received_usdc==0
    assert frozen=={p:p.read_bytes() for p in frozen} and world.mutations==[]


@pytest.mark.parametrize('evidence',['present','incomplete'])
def test_current_owned_presence_blocks_legacy_clean_handoff(combined_world,monkeypatch,capsys,evidence):
    import json
    from dataclasses import replace
    from lp_v2.cli import main
    from lp_v2.journal import Journal
    from lp_v2.models import Order,OrderResult,OrderStatus,Outcome
    from lp_v2.live_control.profiles import atomic_json
    from tests.test_live_sdk import request
    world=combined_world;state=stopped_history(world)
    profile=world.profile;market=profile['trading']['market_condition_id']
    journal=Journal(state/'journal.sqlite');drain=journal.events('stop_result')[-1][1]
    req=request();req=replace(req,owner='lp-v2:'+profile['identity']['wallet_address'].lower(),quote=replace(req.quote,condition_id=market))
    order=Order('current-owned-12345678',req,D(0),D(0),OrderStatus.CANCELLED,drain['at'],True)
    journal.append('order_result',OrderResult(req,Outcome.SUCCESS,order,drain['at']))
    journal.append('stop_result',dict(drain,exit_code=0,reconciliation='SUCCESS',positions=[],orders=[order]));journal.close()
    atomic_json(state/'stop-result.json',dict(run_id='f'*32,exit_code=0))
    route=httpx.HTTPTransport.handle_request;dirty=[True]
    def pages(self,r):
        if r.url.path!='/data/orders':return route(self,r)
        if dirty[0] and evidence=='incomplete':
            if r.url.params.get('next_cursor'):return httpx.Response(503,json={},request=r)
            return httpx.Response(200,json=dict(data=[],next_cursor='later'),request=r)
        return httpx.Response(200,json=dict(data=[dict(id=order.order_id,market=market)] if dirty[0] else [],next_cursor='LTE='),request=r)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',pages)
    seal_fixture_journals(state)
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert main(['live','check'])==(2 if evidence=='incomplete' else 0)
    result=json.loads(capsys.readouterr().out)
    assert 'inventory_handoff' not in result['checks']
    assert result['checks']['legacy_orders']['status']==('UNKNOWN' if evidence=='incomplete' else 'PASS')
    dirty[0]=False
    assert main(['live','check'])==0
    assert 'inventory_handoff' not in json.loads(capsys.readouterr().out)['checks']
    assert world.mutations==[] and frozen=={p:p.read_bytes() for p in frozen}


def retained_handoff_world(world,history='acknowledged'):
    from dataclasses import replace
    from lp_v2.journal import Journal
    from lp_v2.models import Order,OrderResult,OrderStatus,Outcome
    from lp_v2.rotation_state import open_market_journal
    from lp_v2.live_control.profiles import atomic_json
    from tests.test_live_sdk import request
    state=stopped_history(world);p=world.profile
    owner='lp-v2:'+p['identity']['wallet_address'].lower()
    active=Journal(state/'journal.sqlite');drain=active.events('stop_result')[-1][1]
    active.append('stop_result',dict(drain,exit_code=0,reconciliation='SUCCESS',positions=[]))
    active.close();atomic_json(state/'stop-result.json',dict(run_id='f'*32,exit_code=0))
    retained=CID_C if p['trading']['market_condition_id']!=CID_C else BID
    identity=dict(owner=owner,condition_id=retained,token_ids=('201','202'))
    journal=open_market_journal(state,identity)
    req=request();req=replace(req,owner=owner,request_id='retained-request',
        quote=replace(req.quote,condition_id=retained,token_id='201'),
        created_at=drain['at']-timedelta(seconds=1),expires_at=drain['at']+timedelta(hours=1),
        reservation_terms=replace(req.reservation_terms,token_ids=('201','202')))
    journal.append('intent',dict(request=req))
    oid='0x'+'d'*64
    if history=='signed':
        journal.append('signed',dict(request_id=req.request_id,order_ids=(oid,'0x'+'e'*64),token_id='201',
            maker=p['identity']['wallet_address'],signer=p['identity']['signer_address'],
            expiration=int(req.expires_at.timestamp()),signed_at=drain['at']))
    else:
        order=Order(oid,req,D(0),D(0),OrderStatus.CANCELLED,drain['at'],True)
        journal.append('order_result',OrderResult(req,Outcome.SUCCESS,order,drain['at']))
    journal.close()
    seal_fixture_journals(state)
    return state,state/'markets'/retained/'journal.sqlite',oid,retained


@pytest.mark.parametrize('history',['acknowledged','signed'],ids=['acknowledged','signed'])
def test_retained_owned_order_blocks_host_handoff(combined_world,monkeypatch,capsys,history):
    import json
    from lp_v2.cli import main
    world=combined_world;state,path,oid,retained=retained_handoff_world(world,history)
    route=httpx.HTTPTransport.handle_request;dirty=[True]
    def pages(self,r):
        if r.url.path!='/data/orders':return route(self,r)
        return httpx.Response(200,json=dict(data=[dict(id=oid,market=retained)] if dirty[0] else [],next_cursor='LTE='),request=r)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',pages)
    seal_fixture_journals(state)
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert main(['live','check'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['checks']['legacy_orders']['status']=='PASS'
    assert 'inventory_handoff' not in result['checks']
    dirty[0]=False
    assert main(['live','check'])==0
    assert 'inventory_handoff' not in json.loads(capsys.readouterr().out)['checks']
    assert world.mutations==[] and world.readonly is True
    assert all(r.method=='GET' for r in world.http_requests)
    assert frozen=={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}


@pytest.mark.parametrize('problem',['foreign-owner','symlink','unsettled-wal'],ids=['foreign-owner','symlink','unsettled-wal'])
def test_retained_owned_history_fails_closed(combined_world,capsys,problem):
    import json
    from lp_v2.cli import main
    from lp_v2.journal import Journal
    world=combined_world;state,path,oid,retained=retained_handoff_world(world)
    if problem=='foreign-owner':
        history=Journal(path);identity=history.events('identity')[0][1]
        history.append('identity',dict(identity,owner='lp-v2:foreign'));history.close()
        seal_fixture_journals(state)
    elif problem=='symlink':
        destination=path.with_name('retained.sqlite');path.rename(destination);path.symlink_to(destination.name)
    else:
        sidecar=path.with_name(path.name+'-wal');sidecar.write_bytes(b'unsettled');sidecar.chmod(0o600)
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert main(['live','check'])==(0 if problem=='unsettled-wal' else 2)
    report=json.loads(capsys.readouterr().out)
    assert 'inventory_handoff' not in report['checks']
    if problem!='unsettled-wal':assert any(row['status']!='PASS' for row in report['checks'].values())
    assert world.mutations==[] and world.readonly is True
    assert all(r.method=='GET' for r in world.http_requests)
    # SQLite may rebuild its shared-memory cache; durable database/WAL bytes stay unchanged.
    assert {p:v for p,v in frozen.items() if not p.name.endswith('-shm')}=={
        p:p.read_bytes() for p in state.rglob('*') if p.is_file() and not p.name.endswith('-shm')}


def test_settlement_lag_keeps_old_market_owned(tmp_path):
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(clock[0])
    ex.fill('order-1','1','.40',at=clock[0]+timedelta(seconds=1))
    clock[0]+=timedelta(seconds=10);rotation.current.step(clock[0],allow_open=False)
    old=rotation.current;before=old.ledger.snapshot().positions
    assert before[0].quantity==D('1') and before[0].cost_usdc==D('.40')
    ex.quantities['101']=D(0);rotation.scanner.scan_once(clock[0])
    clock[0]+=timedelta(seconds=10);report=rotation.step(clock[0])['rotation']
    assert report['current']==BID and report['entered_count']==1 and report['phase']=='running'
    assert report['blocker']=='UNKNOWN'
    assert rotation.current is not old and old.stop.is_set()
    assert tuple(p.token_id for p in ex.posts)==('101','102','201','202')
    assert old.ledger.snapshot().positions==before and old.sdk.journal.events('exit_observation')
    ex.quantities['101']=D(1)
    for _ in range(3):
        clock[0]+=timedelta(seconds=10);report=rotation.step(clock[0])['rotation']
        assert report['current']==BID and report['entered_count']==1 and report['phase']=='running'
        assert old.stop.is_set() and tuple(p.token_id for p in ex.posts)==('101','102','201','202')
        assert old.ledger.snapshot().positions==before
    assert old.sdk.journal.events('fill')[-1][1].quantity==D(1)
    rotation.close()


def cleared_factory_restart(tmp_path,monkeypatch,clock_mode='advancing'):
    from tests.test_rotation_live import factory_boundary
    from lp_v2.live_runtime import create_live_runner
    config,ex,clock,record,seen=factory_boundary(tmp_path,monkeypatch)
    read_clock=shared_runtime_clock(monkeypatch,clock,clock_mode)
    route=httpx.HTTPTransport.handle_request;unavailable=[True]
    def account(self,r):
        if unavailable[0] and r.url.path=='/transactions':
            return httpx.Response(503,json={},request=r)
        return route(self,r)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',account)
    runner=create_live_runner(config,clock=read_clock)
    runner.scanner.scan_once(clock[0]);runner.step(clock[0])
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.phase=='cleared' and runner.entered_count==0
    assert tuple(p.token_id for p in ex.posts)==TOKENS
    runner.close();unavailable[0]=False
    runner=create_live_runner(config,clock=read_clock);runner.scanner.scan_once(clock[0])
    return runner,ex,clock,record,seen,config


def test_candidate_clean_reads_do_not_count_as_executor_rounds(tmp_path,monkeypatch):
    import json
    from pathlib import Path
    runner,ex,clock,record,seen,config=cleared_factory_restart(tmp_path,monkeypatch)
    old=runner.current;occupation_start=len(old.sdk.journal.events('occupation'))
    ex.sdk.get_balance_allowance.side_effect=TimeoutError('temporary balance unavailable')
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.entered_count==0 and tuple(p.token_id for p in ex.posts)==TOKENS
    ex.sdk.get_balance_allowance.side_effect=ex.balance
    clock[0]+=timedelta(seconds=10);first_at=clock[0];runner.step(first_at)
    assert runner.entered_count==0 and tuple(p.token_id for p in ex.posts)==TOKENS
    def reads(token):
        return sum(r.url.path=='/balance-allowance' and r.url.params.get('asset_type')=='CONDITIONAL'
                   and r.url.params.get('token_id')==token for r in seen)
    before={token:reads(token) for token in ('201','202')}
    runner.step(first_at) # Repeated candidate checks cannot manufacture a fresh executor round.
    assert all(reads(token)==before[token]==0 for token in before)
    assert runner.entered_count==0 and tuple(p.token_id for p in ex.posts)==TOKENS
    before={token:reads(token) for token in ('201','202')}
    clock[0]=first_at+timedelta(seconds=1);runner.step(clock[0])
    assert all(reads(token)==before[token]==0 for token in before)
    assert runner.entered_count==0 and tuple(p.token_id for p in ex.posts)==TOKENS
    clock[0]=first_at+timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID and runner.entered_count==1
    assert tuple(p.token_id for p in ex.posts)==('101','102','201','202')
    proof=next(p['admission_proof'] for _,p in reversed(runner.journal.events('rotation')) if p.get('admission_proof'))
    observations=[p['accounting_as_of'].isoformat() for _,p in old.sdk.journal.events('occupation')[occupation_start:]
                  if p['reconciliation']=='SUCCESS']
    assert proof['source_market']==CID and proof['moments']==observations[-2:]
    assert len(proof['moments'])==2 and proof['moments'][0]<proof['moments'][1]
    assert 'startup_verified' not in json.loads((Path(config.state_dir)/'live-control-run.json').read_text())
    signatures=tuple(p['request_id'] for _,p in runner.sdk.journal.events('signed'))
    assert len(signatures)==len(set(signatures))==2
    runner.step(clock[0]);assert runner.entered_count==1 and len(ex.posts)==4
    runner.close()


def ineligible_old_market(monkeypatch):
    """Real HTTP adapter classification; candidate B remains unchanged."""
    route=httpx.HTTPTransport.handle_request;mode=['closed']
    def handle(self,r):
        response=route(self,r)
        if r.url.path=='/markets/keyset' and r.url.params.get('condition_ids')==CID:
            if mode[0]=='market-network':return httpx.Response(503,json={},request=r)
            data=response.json()
            data['markets']=[dict(row,closed=mode[0]!='market-unknown') if row['conditionId']==CID else row for row in data['markets']]
            return httpx.Response(200,json=data,request=r)
        if r.url.path=='/markets/'+CID and mode[0]=='market-unknown':
            return httpx.Response(200,json=dict(in_play=None),request=r)
        if r.url.path=='/api/geoblock':
            if mode[0]=='region-network':return httpx.Response(503,json={},request=r)
            if mode[0] in ('region-denied','region-unknown','jp-closed-only'):
                return httpx.Response(200,json=dict(blocked=None if mode[0]=='region-unknown' else True,
                    country='JP' if mode[0]=='jp-closed-only' else 'US'),request=r)
        if r.url.path=='/auth/ban-status/closed-only' and mode[0]=='jp-closed-only':
            return httpx.Response(200,json=dict(closed_only=True),request=r)
        return response
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',handle)
    return mode


def candidate_balance_reads(seen):
    return {token:sum(r.url.path=='/balance-allowance' and r.url.params.get('asset_type')=='CONDITIONAL'
                      and r.url.params.get('token_id')==token for r in seen) for token in ('201','202')}


def assert_executor_admission(runner,old,occupation_start):
    proof=next(p['admission_proof'] for _,p in reversed(runner.journal.events('rotation')) if p.get('admission_proof'))
    observations=[p['accounting_as_of'].isoformat() for _,p in old.sdk.journal.events('occupation')[occupation_start:]
                  if p['reconciliation']=='SUCCESS']
    assert proof['source_market']==CID and proof['moments']==observations[-2:]
    assert len(proof['moments'])==2 and proof['moments'][0]<proof['moments'][1]


@pytest.mark.parametrize('clock_mode',['advancing','frozen'],ids=['advancing','frozen'])
def test_cleared_ineligible_executor_rounds_allow_rotation(tmp_path,monkeypatch,clock_mode):
    import json
    from pathlib import Path
    from lp_v2.live_runtime import create_live_runner
    runner,ex,clock,record,seen,config=cleared_factory_restart(tmp_path,monkeypatch,clock_mode)
    ineligible_old_market(monkeypatch)
    old=runner.current;occupation_start=len(old.sdk.journal.events('occupation'))
    first_at=clock[0];runner.step(first_at)
    assert runner.phase=='cleared' and runner.entered_count==0 and tuple(p.token_id for p in ex.posts)==TOKENS
    for at in (first_at,first_at+timedelta(seconds=1)):
        before=candidate_balance_reads(seen);clock[0]=max(clock[0],at);runner.step(at)
        assert all(candidate_balance_reads(seen)[token]==count==0 for token,count in before.items())
        assert runner.entered_count==0 and tuple(p.token_id for p in ex.posts)==TOKENS
    clock[0]=first_at+timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID and runner.entered_count==1
    assert tuple(p.token_id for p in ex.posts)==('101','102','201','202')
    assert_executor_admission(runner,old,occupation_start)
    assert 'startup_verified' not in json.loads((Path(config.state_dir)/'live-control-run.json').read_text())
    signatures=tuple(p['request_id'] for _,p in runner.sdk.journal.events('signed'))
    assert len(signatures)==len(set(signatures))==2
    runner.close();runner=create_live_runner(config,clock=lambda:clock[0])
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID and runner.entered_count==1 and len(ex.posts)==4
    assert tuple(p['request_id'] for _,p in runner.sdk.journal.events('signed'))==signatures
    runner.close()


def entered_ineligible_recovery(tmp_path,monkeypatch):
    runner,ex,clock,record,seen,config=cleared_factory_restart(tmp_path,monkeypatch)
    ineligible_old_market(monkeypatch)
    runner.step(clock[0]);clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID and runner.entered_count==1 and len(ex.posts)==4
    return runner,ex,clock,record,seen,config


@pytest.mark.parametrize('problem',['rotation-phase','rotation-owner','active-identity','run-binding','missing-market','market-unknown'])
def test_saved_rotation_factory_recovery_refuses_untrusted_state(tmp_path,monkeypatch,problem):
    from pathlib import Path
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.journal import Journal
    from lp_v2.live_control.profiles import atomic_json,read_json
    runner,ex,clock,record,seen,config=entered_ineligible_recovery(tmp_path,monkeypatch)
    runner.close();state=Path(config.state_dir)
    if problem in ('rotation-phase','rotation-owner'):
        journal=Journal(state/'rotation.sqlite')
        if problem=='rotation-phase':
            last=journal.events('rotation')[-1][1];journal.append('rotation',dict(last,phase='unknown'))
        else:
            identity=journal.events('rotation_identity')[0][1];journal.append('rotation_identity',dict(identity,owner='lp-v2:foreign'))
        journal.close()
    elif problem=='active-identity':
        journal=Journal(state/'markets'/BID/'journal.sqlite');identity=journal.events('identity')[0][1]
        journal.append('identity',dict(identity,token_ids=('999','998')));journal.close()
    elif problem=='run-binding':
        path=state/'process.json';atomic_json(path,dict(read_json(path),run_id='c'*32))
    elif problem=='missing-market':
        (state/'markets'/BID/'journal.sqlite').unlink()
    else:
        route=httpx.HTTPTransport.handle_request
        def unavailable(self,r):
            if r.url.path=='/markets/keyset' and r.url.params.get('condition_ids')==BID:
                return httpx.Response(503,json={},request=r)
            return route(self,r)
        monkeypatch.setattr(httpx.HTTPTransport,'handle_request',unavailable)
    def facts():
        result={}
        for path in (state/'markets').glob('*/journal.sqlite'):
            journal=Journal(path,readonly=True)
            result[str(path)]=(journal.events('intent'),journal.events('signed'));journal.close()
        return result
    before=facts();signed=ex.sdk.create_limit_order.call_count;posts=len(ex.posts)
    with pytest.raises(ValueError):create_live_runner(config,clock=lambda:clock[0])
    assert facts()==before and ex.sdk.create_limit_order.call_count==signed and len(ex.posts)==posts


def test_fresh_factory_closed_seed_still_fails(tmp_path,monkeypatch):
    from tests.test_rotation_live import factory_boundary
    from lp_v2.live_runtime import create_live_runner
    config,ex,clock,record,seen=factory_boundary(tmp_path,monkeypatch)
    ineligible_old_market(monkeypatch)
    with pytest.raises(ValueError):create_live_runner(config,clock=lambda:clock[0])
    assert not ex.posts and ex.sdk.create_limit_order.call_count==0


@pytest.mark.parametrize('problem',['market-network','market-unknown','region-denied','region-unknown','region-network','jp-closed-only'])
def test_cleared_ineligible_proof_rejects_untrusted_observations(tmp_path,monkeypatch,problem):
    runner,ex,clock,record,seen,config=cleared_factory_restart(tmp_path,monkeypatch)
    mode=ineligible_old_market(monkeypatch);old=runner.current
    runner.step(clock[0])
    assert runner.phase=='cleared' and runner.entered_count==0 and len(ex.posts)==2
    mode[0]=problem
    if problem=='jp-closed-only':ex.sdk.get_closed_only_mode.return_value=True
    occupations=len(old.sdk.journal.events('occupation'))
    for _ in range(2):
        clock[0]+=timedelta(seconds=10);runner.step(clock[0])
        assert runner.entered_count==0 and tuple(p.token_id for p in ex.posts)==TOKENS
    assert len(old.sdk.journal.events('occupation'))==occupations+2
    mode[0]='closed';ex.sdk.get_closed_only_mode.return_value=False
    if runner.phase not in ('cleared','revalidating'):
        # Re-establish actual cleared state; an exiting round is not OQ4 proof.
        clock[0]+=timedelta(seconds=10);runner.step(clock[0])
        assert runner.phase=='cleared' and runner.entered_count==0 and len(ex.posts)==2
    occupation_start=len(old.sdk.journal.events('occupation'))
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.phase=='cleared' and runner.entered_count==0 and len(ex.posts)==2
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID and runner.entered_count==1
    assert tuple(p.token_id for p in ex.posts)==('101','102','201','202')
    assert_executor_admission(runner,old,occupation_start)
    runner.close()


@pytest.mark.parametrize('guard',['action','run','process','window','accounting-failure','restart','owned-open'])
def test_cleared_ineligible_proof_preserves_binding_and_resets(tmp_path,monkeypatch,guard):
    from pathlib import Path
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.live_control.profiles import atomic_json,read_json
    runner,ex,clock,record,seen,config=cleared_factory_restart(tmp_path,monkeypatch)
    ineligible_old_market(monkeypatch);state=Path(config.state_dir)
    process=read_json(state/'process.json');old=runner.current
    runner.step(clock[0])
    assert runner.phase=='cleared' and runner.entered_count==0 and len(ex.posts)==2
    if guard=='restart':
        runner.close();runner=create_live_runner(config,clock=lambda:clock[0]);old=runner.current
        runner.scanner.scan_once(clock[0])
    else:
        if guard=='action':atomic_json(state/'live-control-run.json',dict(record,action_id='c'*32))
        if guard=='run':atomic_json(state/'process.json',dict(process,run_id='c'*32))
        if guard=='process':atomic_json(state/'process.json',dict(process,identity='different-physical-process'))
        if guard=='window':atomic_json(state/'live-control-run.json',dict(record,window_end=(clock[0]+timedelta(minutes=4)).isoformat()))
        if guard=='accounting-failure':ex.sdk.get_balance_allowance.side_effect=TimeoutError('temporary balance unavailable')
        if guard=='owned-open':
            terminal=ex.orders['order-1'];ex.orders['order-1']=terminal.model_copy(update=dict(status='LIVE'))
            ex.cancel_unknown=True
        for _ in range(2):
            clock[0]+=timedelta(seconds=10);runner.step(clock[0])
            assert runner.entered_count==0 and tuple(p.token_id for p in ex.posts)==TOKENS
        atomic_json(state/'live-control-run.json',record);atomic_json(state/'process.json',process)
        ex.sdk.get_balance_allowance.side_effect=ex.balance
        if guard=='owned-open':ex.orders['order-1']=terminal;ex.cancel_unknown=False
        if runner.phase not in ('cleared','revalidating'):
            clock[0]+=timedelta(seconds=10);runner.step(clock[0])
            assert runner.phase=='cleared' and runner.entered_count==0 and len(ex.posts)==2
    occupation_start=len(old.sdk.journal.events('occupation'))
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.phase=='cleared' and runner.entered_count==0 and len(ex.posts)==2
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID and runner.entered_count==1
    assert tuple(p.token_id for p in ex.posts)==('101','102','201','202')
    assert_executor_admission(runner,old,occupation_start)
    signatures=tuple(p['request_id'] for _,p in runner.sdk.journal.events('signed'))
    assert len(signatures)==len(set(signatures))==2
    runner.close()


def test_ineligible_round_before_exit_does_not_self_prove(tmp_path,monkeypatch):
    from tests.test_rotation_live import factory_boundary
    from lp_v2.live_runtime import create_live_runner
    config,ex,clock,record,seen=factory_boundary(tmp_path,monkeypatch)
    read_clock=shared_runtime_clock(monkeypatch,clock,'advancing')
    runner=create_live_runner(config,clock=read_clock)
    runner.scanner.scan_once(clock[0]);runner.step(clock[0])
    assert runner.phase=='running' and runner.condition_id==CID and tuple(p.token_id for p in ex.posts)==TOKENS
    old=runner.current;ineligible_old_market(monkeypatch)
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.phase=='cleared' and runner.entered_count==0 and tuple(p.token_id for p in ex.posts)==TOKENS
    occupation_start=len(old.sdk.journal.events('occupation'))
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.phase=='cleared' and runner.entered_count==0 and len(ex.posts)==2
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID and runner.entered_count==1
    assert tuple(p.token_id for p in ex.posts)==('101','102','201','202')
    assert_executor_admission(runner,old,occupation_start)
    runner.step(clock[0]);assert runner.entered_count==1 and len(ex.posts)==4
    runner.close()


@pytest.mark.parametrize('field',['action','window'],ids=['action','window'])
def test_saved_rotation_factory_rejects_malformed_authorization(tmp_path,monkeypatch,field):
    from dataclasses import replace
    from pathlib import Path
    from tests.test_rotation_live import factory_boundary
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.journal import Journal
    from lp_v2.live_control.profiles import atomic_json
    config,ex,clock,record,seen=factory_boundary(tmp_path,monkeypatch)
    read_clock=shared_runtime_clock(monkeypatch,clock,'advancing')
    runner=create_live_runner(config,clock=read_clock)
    runner.scanner.scan_once(clock[0]);runner.step(clock[0])
    clock[0]+=timedelta(seconds=10);runner.step(clock[0])
    assert runner.condition_id==BID and runner.entered_count==1 and len(ex.posts)==4
    assert runner.drain(timeout_s=0)==0
    runner.close();state=Path(config.state_dir)
    journal=Journal(state/'rotation.sqlite');last=journal.events('rotation')[-1][1]
    journal.append('rotation',dict(last,**({'action_id':'UNKNOWN'} if field=='action' else {'window_end':'UNKNOWN'})))
    journal.close()
    if field=='window':
        clock[0]+=timedelta(seconds=10)
        current=dict(record,action_id='c'*32,run_id='d'*32,started_at=clock[0].isoformat())
        atomic_json(state/'live-control-run.json',current)
        atomic_json(state/'process.json',dict(run_id='d'*32,pid=1234,identity='fake-boot:123'))
        config=replace(config,started_at=clock[0])
    def facts():
        result={}
        for path in (state/'markets').glob('*/journal.sqlite'):
            journal=Journal(path,readonly=True)
            result[str(path)]=(journal.events('intent'),journal.events('signed'));journal.close()
        return result
    before=facts();posts=len(ex.posts);signed=ex.sdk.create_limit_order.call_count;restored=None
    try:
        with pytest.raises(ValueError):restored=create_live_runner(config,clock=read_clock)
    finally:
        if restored is not None:restored.close()
    assert facts()==before and len(ex.posts)==posts and ex.sdk.create_limit_order.call_count==signed


def active_market_recovery_http(monkeypatch,ex,market):
    route=httpx.HTTPTransport.handle_request
    state=dict(market=market,region='allowed',seed_closed=False)
    def handle(self,r):
        response=route(self,r)
        cid=r.url.params.get('condition_ids')
        if r.url.path=='/markets/keyset' and cid in (CID,BID):
            if cid==BID and state['market']=='network':return httpx.Response(503,json={},request=r)
            data=response.json()
            data['markets']=[dict(row,closed=(state['seed_closed'] if row['conditionId']==CID else state['market']=='closed'))
                             for row in data['markets']]
            return httpx.Response(200,json=data,request=r)
        if r.url.path=='/markets/'+BID:
            return httpx.Response(200,json=dict(in_play=None if state['market']=='unknown' else state['market']=='in_play'),request=r)
        if r.url.path=='/api/geoblock' and state['region']!='allowed':
            if state['region']=='network':return httpx.Response(503,json={},request=r)
            return httpx.Response(200,json=dict(blocked=None if state['region']=='unknown' else True,
                country='JP' if state['region']=='jp-closed-only' else 'US'),request=r)
        if r.url.path=='/auth/ban-status/closed-only' and state['region']=='jp-closed-only':
            return httpx.Response(200,json=dict(closed_only=True),request=r)
        if r.url.path=='/balance-allowance' and r.url.params.get('asset_type')=='CONDITIONAL':
            return httpx.Response(200,json=dict(balance=str(int(ex.quantities.get(r.url.params['token_id'],D(0))*1000000)),allowances={}),request=r)
        return response
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',handle)
    return state


def active_recovery_world(tmp_path,monkeypatch,phase,market,*,filled=False):
    from tests.test_rotation_live import factory_boundary
    from lp_v2.live_runtime import create_live_runner
    config,ex,clock,record,seen=factory_boundary(tmp_path,monkeypatch)
    read_clock=shared_runtime_clock(monkeypatch,clock,'advancing')
    runner=create_live_runner(config,clock=read_clock)
    try:
        runner.scanner.scan_once(clock[0]);runner.step(clock[0])
        clock[0]+=timedelta(seconds=10);runner.step(clock[0])
        assert runner.condition_id==BID and runner.entered_count==1 and len(ex.posts)==4
        state=active_market_recovery_http(monkeypatch,ex,market)
        if filled:
            own_id=next(result.order.order_id for _,result in runner.sdk.journal.events('order_result')
                if result.order is not None and result.request.quote.condition_id==BID
                and result.request.quote.token_id=='201' and result.request.purpose=='opening')
            ex.fill(own_id,'1','.40',at=clock[0]+timedelta(seconds=1))
            trade=ex.trades[-1];wallet=runner.sdk.wallet
            ex.trades[-1]=trade.model_copy(update=dict(maker_address=wallet,
                maker_orders=[m.model_copy(update=dict(maker_address=wallet)) for m in trade.maker_orders]))
            if phase=='exiting':ex.cancel_unknown=True
            clock[0]+=timedelta(seconds=10);runner.current.step(clock[0],allow_open=False)
            position=runner.current.ledger.snapshot().positions[0]
            assert position.quantity==D(1) and position.cost_usdc==D('.40')
        if phase=='exiting':
            ex.cancel_unknown=True;clock[0]+=timedelta(seconds=10);runner.step(clock[0])
        assert runner.phase==phase
        ex.orders['foreign']=ex.orders['order-3'].model_copy(update=dict(id='foreign',maker_address='0x'+'9'*40))
        runner.close();ex.cancel_unknown=False
        return ex,clock,record,seen,config,state
    except BaseException:
        runner.close();raise


@pytest.mark.parametrize('phase,market',[('running','closed'),('running','in_play'),('exiting','closed'),('exiting','in_play')],
    ids=['running-closed','running-in_play','exiting-closed','exiting-in_play'])
def test_ineligible_active_restart_drains_without_replay(tmp_path,monkeypatch,phase,market):
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.journal import Journal
    from pathlib import Path
    ex,clock,record,seen,config,state=active_recovery_world(tmp_path,monkeypatch,phase,market)
    path=Path(config.state_dir)/'markets'/BID/'journal.sqlite'
    journal=Journal(path,readonly=True);before=(journal.events('intent'),journal.events('signed'));journal.close()
    runner=None
    try:
        runner=create_live_runner(config,clock=lambda:clock[0])
        for _ in range(3):
            clock[0]+=timedelta(seconds=10);report=runner.step(clock[0])['rotation']
            assert runner.condition_id==BID and runner.entered_count==1 and len(ex.posts)==4
            assert ex.orders['foreign'].status=='LIVE'
        assert report['phase']=='cleared' and report['reason']=='no_executable_candidate'
        assert report['recovery_mode']=='normal'
        assert ex.orders['order-3'].status==ex.orders['order-4'].status=='CANCELED'
        assert (runner.sdk.journal.events('intent'),runner.sdk.journal.events('signed'))==before
        runner.close();runner=None
        runner=create_live_runner(config,clock=lambda:clock[0])
        state['market']='eligible'
        for _ in range(3):
            clock[0]+=timedelta(seconds=10);report=runner.step(clock[0])['rotation']
            assert runner.condition_id==BID and runner.entered_count==1 and len(ex.posts)==4
            assert report['reason']=='no_executable_candidate' and report['recovery_mode']=='normal'
            assert ex.orders['foreign'].status=='LIVE'
        assert (runner.sdk.journal.events('intent'),runner.sdk.journal.events('signed'))==before
    finally:
        if runner is not None:runner.close()


@pytest.mark.parametrize('phase,market',[('running','closed'),('running','in_play'),('exiting','closed'),('exiting','in_play')],
    ids=['running-closed','running-in_play','exiting-closed','exiting-in_play'])
def test_ineligible_active_restart_preserves_owned_inventory(tmp_path,monkeypatch,phase,market):
    from lp_v2.live_runtime import create_live_runner
    ex,clock,record,seen,config,state=active_recovery_world(tmp_path,monkeypatch,phase,market,filled=True)
    runner=None
    try:
        runner=create_live_runner(config,clock=lambda:clock[0])
        original_openings=tuple(p['request'].request_id for _,p in runner.sdk.journal.events('intent') if p['request'].purpose=='opening')
        fills=runner.sdk.journal.events('fill');foreign=ex.orders['foreign']
        clock[0]+=timedelta(seconds=10);report=runner.step(clock[0])['rotation']
        position=runner.current.ledger.snapshot().positions[0]
        assert position.condition_id==BID and position.token_id=='201'
        assert position.quantity==D(1) and position.cost_usdc==D('.40')
        assert report['phase']=='cleared' and report['recovery_mode']=='normal'
        assert runner.condition_id==BID and runner.entered_count==1 and len(ex.posts)==4
        ex.quantities['201']=D(0)
        clock[0]+=timedelta(seconds=10);report=runner.step(clock[0])['rotation']
        assert report['blocker']['code']=='no_executable_candidate'
        assert report['blocker']['numbers']=='UNKNOWN'
        assert runner.current.ledger.snapshot().positions[0]==position
        assert report['phase']=='cleared' and runner.entered_count==1 and len(ex.posts)==4
        ex.quantities['201']=D(1)
        clock[0]+=timedelta(seconds=10);report=runner.step(clock[0])['rotation']
        assert runner.current.ledger.snapshot().positions[0]==position
        assert report['phase']=='cleared' and len(ex.posts)==4
        assert runner.sdk.journal.events('fill')==fills
        state['market']='eligible'
        clock[0]+=timedelta(seconds=10);runner.step(clock[0])
        requests=tuple(p['request'] for _,p in runner.sdk.journal.events('intent'))
        assert tuple(r.request_id for r in requests if r.purpose=='opening')==original_openings
        assert not any(r.purpose=='hedge' for r in requests)
        assert all(r.purpose in ('opening','hedge') and r.quote.condition_id==BID for r in requests)
        assert runner.entered_count==1 and runner.condition_id==BID
        assert runner.current.ledger.snapshot().positions[0]==position
        assert ex.orders['foreign']==foreign
    finally:
        if runner is not None:runner.close()


def owned_execution_facts(state):
    from pathlib import Path
    from lp_v2.journal import Journal
    result={}
    for path in (Path(state)/'markets').glob('*/journal.sqlite'):
        journal=Journal(path,readonly=True)
        result[str(path)]=(journal.events('intent'),journal.events('signed'));journal.close()
    return result


@pytest.mark.parametrize('problem',['market-network','market-unknown','region-denied','region-unknown','region-network','jp-closed-only','new-on-closed-seed'])
def test_ineligible_active_factory_refuses_untrusted_recovery(tmp_path,monkeypatch,problem):
    from dataclasses import replace
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.live_control.profiles import atomic_json
    from pathlib import Path
    ex,clock,record,seen,config,state=active_recovery_world(tmp_path,monkeypatch,'running','closed')
    if problem in ('market-network','market-unknown'):
        state['market']='network' if problem=='market-network' else 'unknown'
    elif problem in ('region-denied','region-unknown','region-network'):
        state['region']=problem.removeprefix('region-')
    elif problem=='jp-closed-only':
        state['region']='jp-closed-only';ex.sdk.get_closed_only_mode.return_value=True
    else:
        state['seed_closed']=True;clock[0]+=timedelta(seconds=10)
        path=Path(config.state_dir)
        current=dict(record,action_id='c'*32,run_id='d'*32,started_at=clock[0].isoformat())
        atomic_json(path/'live-control-run.json',current)
        atomic_json(path/'process.json',dict(run_id='d'*32,pid=1234,identity='fake-boot:123'))
        config=replace(config,started_at=clock[0])
    before=owned_execution_facts(config.state_dir);posts=len(ex.posts);signed=ex.sdk.create_limit_order.call_count
    runner=None
    try:
        with pytest.raises(ValueError):runner=create_live_runner(config,clock=lambda:clock[0])
    finally:
        if runner is not None:runner.close()
    assert owned_execution_facts(config.state_dir)==before
    assert len(ex.posts)==posts and ex.sdk.create_limit_order.call_count==signed


@pytest.mark.parametrize('field',['mode','action','run','window','market'])
def test_exit_only_recovery_rejects_corrupt_binding(tmp_path,monkeypatch,field):
    from pathlib import Path
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.journal import Journal
    ex,clock,record,seen,config,state=active_recovery_world(tmp_path,monkeypatch,'running','closed')
    recovered=create_live_runner(config,clock=lambda:clock[0])
    recovered.close();state['market']='eligible'
    journal=Journal(Path(config.state_dir)/'rotation.sqlite');fact=journal.events('recovery_mode')[-1][1]
    changed=dict(fact)
    if field=='mode':changed['mode']='UNKNOWN'
    if field=='action':changed['action_id']='c'*32
    if field=='run':changed['run_id']='c'*32
    if field=='window':changed['window_end']=fact['window_end']+timedelta(seconds=1)
    if field=='market':changed['market']=CID
    journal.append('recovery_mode',changed);journal.close()
    before=owned_execution_facts(config.state_dir);posts=len(ex.posts);signed=ex.sdk.create_limit_order.call_count
    runner=None
    try:
        with pytest.raises(ValueError):runner=create_live_runner(config,clock=lambda:clock[0])
    finally:
        if runner is not None:runner.close()
    assert owned_execution_facts(config.state_dir)==before
    assert len(ex.posts)==posts and ex.sdk.create_limit_order.call_count==signed
