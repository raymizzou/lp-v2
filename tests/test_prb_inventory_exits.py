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
    obs.read_conditional_inventory=lambda now,cid:dict(data,as_of=now.isoformat(),observed_at=now.isoformat())
    rotation.step(AT)
    clock[0]+=timedelta(seconds=10);rotation.scanner.scan_once(clock[0])
    report=rotation.step(clock[0])['rotation']
    rows=report['blocker']['numbers']['inventory']
    assert [(r['exchange'],r['ledger'],r['difference']) for r in rows]==[('1.234567','0','1.234567'),('0','0','0')]
    assert rows[0]['explanation']=='exchange_balance_differs_from_historical_ledger'
    assert rows[0]['source']==data['source'] and rows[0]['observed_at']==clock[0].isoformat()
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


def test_zero_exchange_balance_exits_with_auditable_ledger_warning(tmp_path):
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
    assert report['phase']=='cleared' and report['entered_count']==0
    assert old.ledger.snapshot().positions==before
    event=old.sdk.journal.events('exit_observation')[-1][1]
    assert event['accounting']=='UNRECONCILED'
    assert event['numbers']['inventory'][0]['exchange']==0
    assert event['numbers']['inventory'][0]['ledger']==1
    assert event['numbers']['inventory'][0]['difference']==-1
    assert event['numbers']['inventory'][0]['source']=='UNKNOWN'
    assert report['exit_observation']['accounting']=='UNRECONCILED'
    assert old.sdk.journal.events('fill')[-1][1].quantity==1
    assert {p.token_id for p in ex.posts}<=set(TOKENS)
    rotation.close()


def test_nonzero_inventory_blocker_keeps_complete_evidence(tmp_path):
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(AT)
    ex.quantities['101']=D('.008133')
    clock[0]+=timedelta(seconds=10);rotation.scanner.scan_once(clock[0])
    first=clock[0]
    report=rotation.step(clock[0])['rotation']
    for elapsed in (0,3599,3600):
        if elapsed:
            clock[0]=first+timedelta(seconds=elapsed);report=rotation.step(clock[0])['rotation']
        row,other=report['blocker']['numbers']['inventory']
        assert (row['exchange'],row['ledger'],row['difference'])==('0.008133','0','0.008133')
        assert row['upstream_unit']==row['upstream_precision']=='UNKNOWN'
        assert other['exchange']=='0' and row['explanation']=='exchange_balance_differs_from_historical_ledger'
        assert report['blocker']['escalated']==(elapsed==3600)
        assert 'externally' in report['blocker']['next_step']
        assert report['phase']=='exiting' and report['entered_count']==0
    ex.quantities['101']=D('0');clock[0]+=timedelta(seconds=10)
    report=rotation.step(clock[0])['rotation']
    assert report['phase']=='cleared' and report['reason']=='needs_new_on'
    assert len(ex.posts)==2
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
    clock[0]+=timedelta(seconds=10);runner.current.step(clock[0],allow_open=False)
    old=runner.current;before=old.ledger.snapshot().positions
    ex.quantities['101']=D(0)
    clock[0]+=timedelta(seconds=10);runner.scanner.scan_once(clock[0]);report=runner.step(clock[0])['rotation']
    assert report['phase']=='cleared' and report['entered_count']==0
    assert {s.token_id for s in ex.posts}<=set(TOKENS)
    runner.step(clock[0]);assert runner.entered_count==0
    clock[0]+=timedelta(seconds=10);report=runner.step(clock[0])['rotation']
    assert report['current']==BID and report['entered_count']==1
    assert old.ledger.snapshot().positions==before
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


from tests.test_live_control_host import combined_world, target, operator_os
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
    return state


def test_clean_live_handoff_ignores_previous_exit_one(combined_world,capsys):
    from lp_v2.cli import main
    world=combined_world;state=stopped_history(world)
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert main(['live','check'])==0
    import json
    report=json.loads(capsys.readouterr().out)
    assert report['checks']['inventory_handoff']['status']=='PASS'
    assert report['checks']['inventory_handoff']['reason']=='FRESH_ZERO_EXECUTION_RISK_CLEARED'
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
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert main(['live','check'])==0
    assert json.loads(capsys.readouterr().out)['checks']['inventory_handoff']['status']=='PASS'
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
    assert main(['live','check'])==2
    report=json.loads(capsys.readouterr().out);row=report['checks']['inventory_handoff']
    assert row['status']==('UNKNOWN' if dirty=='merge' else 'FAIL')
    first,second=row['numbers']['inventory']
    assert first['exchange']==('0.008133' if dirty=='balance' else '0')
    assert first['ledger']=='1.25'
    assert first['difference']==('-1.241867' if dirty=='balance' else '-1.25')
    assert first['upstream_unit']=='UNKNOWN' and second['exchange']=='0'
    assert row['owned_open_count']==(1 if dirty=='order' else 0)
    assert row['merge_count']==(1 if dirty=='merge' else 0)
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
    assert report['reason']=='needs_new_on' and len(ex.posts)==2
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


@pytest.mark.parametrize('guard',['action','run','process','window','failed-round','duplicate','restart'])
def test_self_proof_preserves_current_binding_and_consecutive_rounds(tmp_path,monkeypatch,guard):
    import json
    from pathlib import Path
    from tests.test_rotation_live import factory_boundary
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.live_control.profiles import atomic_json
    from lp_v2.readonly_sdk import FixedMarketIneligible
    config,ex,clock,record,seen=factory_boundary(tmp_path,monkeypatch)
    state=Path(config.state_dir)
    original_process=json.loads((state/'process.json').read_text())
    runner=create_live_runner(config,clock=lambda:clock[0]);runner.scanner.scan_once(clock[0]);runner.step(clock[0])
    assert runner.condition_id==CID and tuple(p.token_id for p in ex.posts)==TOKENS
    if guard=='duplicate':
        runner.step(clock[0]);runner.step(clock[0])
    elif guard=='restart':
        runner.close();runner=create_live_runner(config,clock=lambda:clock[0]);runner.scanner.scan_once(clock[0])
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
    # Once authorization expires, confirmed current ineligibility may clear old
    # execution but cannot admit any fresh candidate.
    clock[0]=runner.window_end
    def excluded(now,cid):raise FixedMarketIneligible('confirmed closed')
    runner.observations.read_fixed_market=excluded
    signed=len(ex.posts);report=runner.step(clock[0])['rotation']
    assert report['reason']=='needs_new_on' and report['entered_count']==1 and len(ex.posts)==signed
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
    if variant=='unexpired':current.drain(timeout_s=0)
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
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    code=main(['live','check']);result=json.loads(capsys.readouterr().out)
    handoff=result['checks']['inventory_handoff']
    if variant=='clean':
        assert code==0 and handoff['status']=='PASS', (handoff,len(observations),{k:v['status'] for k,v in result['checks'].items()})
        assert len(observations)>=3 and observations[-1]>observations[-2]
    else:assert code==2 and handoff['status']!='PASS'
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
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert main(['live','check'])==2
    result=json.loads(capsys.readouterr().out);row=result['checks']['inventory_handoff']
    if evidence=='present':
        assert result['checks']['legacy_orders']['status']=='PASS'
        assert row['status']=='FAIL' and row['owned_open_count']==1
    else:assert row['status']=='UNKNOWN'
    assert [r['exchange'] for r in row['numbers']['inventory']]==['0','0']
    assert [r['ledger'] for r in row['numbers']['inventory']]==['0','0']
    dirty[0]=False
    assert main(['live','check'])==0
    assert json.loads(capsys.readouterr().out)['checks']['inventory_handoff']['status']=='PASS'
    assert world.mutations==[] and frozen=={p:p.read_bytes() for p in frozen}
