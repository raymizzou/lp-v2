from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from tests.test_live_runtime import world, inputs
from tests.test_live_sdk import AT, CID, TOKENS, OWNER, WALLET, SIGNER
from tests.test_readonly_sdk import step_at


def test_exit_only_cancels_opening_but_keeps_hedges(tmp_path):
    runner, ex, sdk, journal, obs = world(tmp_path)
    runner.step(AT)
    original = len(ex.posts)
    runner.clock=lambda:AT+timedelta(seconds=10)
    runner.step(AT+timedelta(seconds=10), allow_open=False)
    assert {call.kwargs['order_id'] for call in ex.sdk.cancel_order.call_args_list} == {'order-1','order-2'}
    assert len(ex.posts) == original
    assert all(o.status == 'CANCELED' for o in ex.orders.values())
    sdk.close()

    hedge_dir=tmp_path/'hedge';hedge_dir.mkdir()
    runner, ex, sdk, journal, obs = world(hedge_dir)
    ex.reject_tokens.add('102')
    runner.step(AT)
    ex.fill('order-1','10','.40',at=AT+timedelta(seconds=1))
    ex.reject_tokens.clear()
    obs.plans=(inputs(no='.60'),)
    runner.clock=lambda:AT+timedelta(seconds=10)
    runner.step(AT+timedelta(seconds=10),allow_open=False)
    hedges=[r for r in runner.ledger.snapshot().requests if r.purpose=='hedge']
    assert len(hedges)==1 and hedges[0].quote.price == D('.57')
    assert len([r for r in runner.ledger.snapshot().requests if r.purpose=='opening'])==2
    runner.clock=lambda:AT+timedelta(seconds=20)
    runner.step(AT+timedelta(seconds=20),allow_open=False)
    assert ex.orders['order-3'].status == 'LIVE'
    ex.fill('order-3','10','.57',at=AT+timedelta(seconds=21))
    runner.clock=lambda:AT+timedelta(seconds=30)
    runner.step(AT+timedelta(seconds=30),allow_open=False)
    assert len(journal.events('merge_intent'))==1
    assert journal.events('merge_intent')[0][1]['request'].quantity==D('10')
    assert len(ex.posts)==3
    sdk.close()


BID='0x'+'b'*64
CID_C='0x'+'c'*64


def rotation_plan(cid=CID, *, reward=100, tokens=TOKENS):
    p=inputs(yes='.40',no='.40',reward=reward)
    return replace(p,market=replace(p.market,condition_id=cid,tokens=tuple(
        replace(t,token_id=token) for t,token in zip(p.market.tokens,tokens))),
        books=tuple(replace(b,condition_id=cid,token_id=token) for b,token in zip(p.books,tokens)),
        fee_per_share=(D('0'),D('0')))


class RotationReads:
    """Fake public/account exchange reads only; runners/ledger/journals are real."""
    def __init__(self, exchange, clock):
        self.exchange,self.clock=exchange,clock
        self.plans={CID:rotation_plan(),BID:rotation_plan(BID,reward=200,tokens=('201','202')),
                    CID_C:rotation_plan(CID_C,reward=300,tokens=('301','302'))}
        self.ranked=(CID,BID)
        self.complete=True
        self.fail_public=set();self.fail_account=False;self.inventory_unknown=False
        self.region=dict(blocked=False,country='US')
        self.reads=[]
    def scan_markets(self, now):
        from lp_v2.rotation_scan import ScanRound
        return ScanRound(tuple(self.read_fixed_market(now,cid)[0] for cid in self.ranked),dict(
            started_at=now,finished_at=now,duration_s=0,directory_duration_s=0,market_duration_s=0,
            pages=1,rows=len(self.ranked),requests=1,prescreened=len(self.ranked),
            reached_end=self.complete,errors=[] if self.complete else [dict(page=2,reason='cursor_invalid')]))
    def read_fixed_market(self,now,cid):
        from lp_v2.scoring import eligible_market
        self.reads.append(('books',cid,now))
        p=self.plans[cid]
        p=replace(p,books=tuple(replace(b,observed_at=now) for b in p.books))
        return [dict(condition_id=cid,title=cid,eligible=eligible_market(p.market),plan=p,
                     score='1',observation_completed_at=now)]
    def read_region_policy(self):return self.region
    def read_conditional_inventory(self,now,cid):
        if self.inventory_unknown:raise ValueError('unavailable')
        tokens=tuple(t.token_id for t in self.plans[cid].market.tokens)
        return dict(condition_id=cid,token_ids=tokens,quantities={t:self.exchange.quantities.get(t,D('0')) for t in tokens},as_of=now.isoformat())
    def public_preflight(self,now,cid):
        self.reads.append(('public_check',cid,now))
        from lp_v2.live_control.workflow import PUBLIC
        return {key:dict(status='BLOCKED' if cid in self.fail_public and key=='books' else 'PASS') for key in PUBLIC}
    def read_account_readiness(self,now,**kwargs):
        self.reads.append(('account_check',None,now))
        return {key:dict(status='FAIL' if self.fail_account and key=='balance' else 'PASS') for key in
                ('balance','allowance','closed_only','legacy_orders','region','relayer_auth','proxy_execution')}
    def close(self):pass


def rotation_world(tmp_path, *, now=None, identity=None, code_sha="UNKNOWN"):
    from lp_v2.journal import Journal
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.rotation_scan import ScanWorker
    from lp_v2.rotation_live import RotationRunner
    from lp_v2.cli import RunConfig
    from tests.fakes.live_exchange import Exchange
    clock=[now or AT]
    ex=Exchange()
    ex.token_markets={'101':CID,'102':CID,'201':BID,'202':BID,'301':CID_C,'302':CID_C}
    post=ex.post
    def multi_post(signed):
        ex.condition_id=ex.token_markets[signed.token_id]
        return post(signed)
    ex.sdk.post_order.side_effect=multi_post
    path=tmp_path/'markets'/CID;path.mkdir(parents=True,exist_ok=True)
    journal=Journal(path/'journal.sqlite')
    wallet=(identity or {}).get('wallet_address',WALLET)
    signer=(identity or {}).get('signer_address',SIGNER)
    owner='lp-v2:'+wallet.lower()
    sign=ex.sign
    ex.sdk.create_limit_order.side_effect=lambda **kw:replace(sign(**kw),maker=wallet,signer=signer)
    original_multi=multi_post
    def owned_post(signed):
        response=original_multi(signed)
        if hasattr(response,'order_id') and response.order_id in ex.orders:
            ex.orders[response.order_id]=ex.orders[response.order_id].model_copy(update={'maker_address':wallet})
        return response
    ex.sdk.post_order.side_effect=owned_post
    identity=dict(owner=owner,condition_id=CID,token_ids=TOKENS)
    if not journal.events('identity'):journal.append('identity',identity)
    sdk=LiveSDK(ex.sdk,journal,**identity,wallet=wallet,signer=signer,clock=lambda:clock[0])
    obs=RotationReads(ex,clock)
    config=RunConfig(D('100'),clock[0],state_dir=str(tmp_path),market=CID,code_sha=code_sha)
    current=LiveRunner(config,sdk,obs,clock=lambda:clock[0])
    scanner=ScanWorker(obs,clock=lambda:clock[0])
    rotation=RotationRunner(config,current,scanner,action_id='on-action',window_end=clock[0]+timedelta(minutes=5),
                            clock=lambda:clock[0],account_identity=dict(signer_address=signer,wallet_address=wallet,signature_type=1),relayer={})
    return rotation,ex,obs,clock


def test_late_fill_prevents_new_market(tmp_path):
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(AT)
    original_cancel=ex.cancel
    def late_cancel(*,order_id):
        response=original_cancel(order_id=order_id)
        if order_id=='order-1':
            ex.condition_id=CID
            ex.fill(order_id,'4','.40',at=AT+timedelta(seconds=11))
        return response
    ex.sdk.cancel_order.side_effect=late_cancel
    clock[0]=AT+timedelta(seconds=10)
    rotation.scanner.scan_once(clock[0])
    rotation.step(clock[0])
    clock[0]=AT+timedelta(seconds=20)
    report=rotation.step(clock[0])
    assert report['rotation']['phase']=='exiting'
    assert report['rotation']['current']==CID
    assert {signed.token_id for signed in ex.posts} <= set(TOKENS)
    assert len(ex.posts)==3 and ex.posts[-1].token_id=='102'
    hedge=rotation.current.ledger.snapshot().requests[-1]
    assert hedge.purpose=='hedge' and hedge.quote.quantity==D('4')
    assert hedge.quote.price<=D('.58')
    assert report['rotation']['reason']=='waiting_inventory'


@pytest.mark.parametrize('risk', ['inventory','pending_order','pending_cancel','unknown_order','unknown_cancel',
    'unknown_merge','unresolved_merge_history','chain_discrepancy','dust','unaffordable_hedge'])
def test_uncertain_old_risk_blocks_entry(tmp_path,risk):
    rotation,ex,obs,clock=rotation_world(tmp_path)
    if risk=='pending_order':
        ex.sdk.post_order.side_effect=TimeoutError('uncertain post')
    rotation.step(AT)
    if risk in ('inventory','dust','unaffordable_hedge'):
        ex.fill('order-1','.5' if risk=='dust' else '4','.40',at=AT+timedelta(seconds=1))
        if risk=='unaffordable_hedge':
            obs.plans[CID]=replace(obs.plans[CID],books=(obs.plans[CID].books[0],
                replace(obs.plans[CID].books[1],best_bid=D('.90'),best_ask=D('.92'))))
    if risk in ('pending_cancel','unknown_cancel'):ex.cancel_unknown=True
    if risk=='unknown_order':ex.sdk.get_order.side_effect=TimeoutError('unavailable')
    if risk in ('unknown_merge','unresolved_merge_history'):
        ex.fill('order-1','10','.40',at=AT+timedelta(seconds=1))
        ex.fill('order-2','10','.40',at=AT+timedelta(seconds=2))
        ex.sdk.merge_positions.side_effect=TimeoutError('uncertain merge')
    if risk=='chain_discrepancy':ex.quantities['101']=D('1')
    clock[0]=AT+timedelta(seconds=10)
    rotation.scanner.scan_once(clock[0])
    rotation.step(clock[0])
    clock[0]=AT+timedelta(seconds=20)
    report=rotation.step(clock[0])
    assert report['rotation']['phase']=='exiting'
    assert report['rotation']['current']==CID
    assert {s.token_id for s in ex.posts} <= set(TOKENS)
    expected=('waiting_pending' if risk=='pending_order' else 'waiting_orders' if risk in ('pending_cancel','unknown_cancel','unknown_order')
              else 'waiting_merge' if risk in ('unknown_merge','unresolved_merge_history') else 'waiting_readback' if risk=='chain_discrepancy'
              else 'waiting_inventory')
    assert report['rotation']['reason']==expected
    if risk=='unaffordable_hedge':
        hedge=rotation.current.ledger.snapshot().requests[-1]
        assert hedge.purpose=='hedge' and hedge.quote.price==D('.58')


def test_normal_rotation_records_and_preserves_journals(tmp_path):
    from lp_v2.journal import Journal
    from lp_v2.scoring import SCORE_VERSION
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(AT)
    old=rotation.current.sdk.journal
    prefix=old.events()
    clock[0]=AT+timedelta(seconds=10)
    rotation.scanner.scan_once(clock[0])
    report=rotation.step(clock[0])
    assert report['rotation']['current']==BID and report['rotation']['phase']=='running'
    assert {s.token_id for s in ex.posts[2:]}=={'201','202'}
    assert report['rotation']['cancel_count']==2
    assert old.events()[:len(prefix)]==prefix
    assert old.events('stop_result')[-1][1]['exit_code']==0
    assert (tmp_path/'markets'/CID/'journal.sqlite').exists()
    assert (tmp_path/'markets'/BID/'journal.sqlite').exists()
    events=rotation.journal.events('rotation')
    assert [e['phase'] for _,e in events]==['running','exiting','cleared','revalidating','running']
    for _,event in events:
        assert set(('old_market','new_market','reason','score_at','score_version','scan_started_at',
                    'scan_finished_at','action_id','window_end','phase','result','at'))<=event.keys()
        assert event['score_version']==SCORE_VERSION
        assert event['action_id']=='on-action'
        assert event['window_end']==AT+timedelta(minutes=5)
    assert events[-1][1]['old_market']==CID and events[-1][1]['new_market']==BID
    assert events[-1][1]['result']=='PASS'
    assert ('books',BID,clock[0]) in obs.reads and ('public_check',BID,clock[0]) in obs.reads
    assert ('account_check',None,clock[0]) in obs.reads
    with pytest.raises(Exception):
        rotation.journal.db.execute("DELETE FROM events")


@pytest.mark.parametrize('revalidation', ['latest','skip_first','all_public_fail','account_fail'])
def test_latest_complete_target_and_revalidation(tmp_path,revalidation):
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(AT)
    ex.cancel_unknown=True
    clock[0]=AT+timedelta(seconds=10);rotation.scanner.scan_once(clock[0]);rotation.step(clock[0])
    assert rotation.snapshot()['rotation']['phase']=='exiting'
    obs.ranked=(CID,BID,CID_C)
    clock[0]=AT+timedelta(seconds=20);rotation.scanner.scan_once(clock[0])
    # Later partial ranking cannot displace the last complete target at entry.
    obs.complete=False;obs.ranked=(CID,BID)
    clock[0]+=timedelta(seconds=1)
    rotation.scanner.scan_once(clock[0])
    if revalidation=='skip_first':obs.fail_public={CID_C}
    if revalidation=='all_public_fail':obs.fail_public={CID_C,BID,CID}
    if revalidation=='account_fail':obs.fail_account=True
    ex.cancel_unknown=False
    for oid in ex.orders:ex.terminal(oid)
    report=rotation.step(clock[0])
    expected=CID_C if revalidation=='latest' else BID if revalidation=='skip_first' else None
    assert report['rotation']['current']==expected
    assert ('books',CID_C,clock[0]) in obs.reads
    if expected:
        assert report['rotation']['phase']=='running'
        entered=rotation.journal.events('rotation')[-1][1]
        assert entered['scan_started_at']==entered['scan_finished_at']==AT+timedelta(seconds=20)
        cleared=next(event for _,event in rotation.journal.events('rotation') if event['phase']=='cleared')
        assert cleared['new_market']==CID_C
        assert {s.token_id for s in ex.posts[2:]}==set(t.token_id for t in obs.plans[expected].market.tokens)
        assert all(0 <= (clock[0]-b.observed_at).total_seconds() <= 60 for p in rotation.current._last_plans for b in p.books)
    else:
        assert len(ex.posts)==2
        assert report['rotation']['reason']=='no_revalidated_candidate'
        assert report['rotation']['phase']=='cleared'


@pytest.mark.parametrize('offset', [-1,0,1])
def test_first_signature_window_guard(tmp_path,offset):
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(AT)
    clock[0]=AT+timedelta(seconds=10);rotation.scanner.scan_once(clock[0])
    original_read=obs.read_fixed_market
    count=[0]
    def read(now,cid):
        if cid==BID:
            count[0]+=1
            if count[0]==2:
                clock[0]=rotation.window_end+timedelta(seconds=offset)
        return original_read(now,cid)
    obs.read_fixed_market=read
    rotation.step(clock[0])
    new=ex.posts[2:]
    assert len(new)==(2 if offset<0 else 0)
    new_sdk=rotation.current.sdk
    if offset<0:
        records=[p for _,p in new_sdk.journal.events('signed')]
        assert len(records)==2
        assert all(p['signed_at']<rotation.window_end and p['action_id']=='on-action' for p in records)
        assert all(p['window_end']==rotation.window_end for p in records)
        # Once the entry has actually signed, current-market execution may continue.
        clock[0]=rotation.window_end+timedelta(seconds=20)
        rotation.step(clock[0])
        assert rotation.snapshot()['rotation']['current']==BID
    else:
        assert not new_sdk.journal.events('signed')
        assert ex.sdk.create_limit_order.call_count==2


def test_expired_window_invalid_current_exits_to_idle(tmp_path):
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(AT)
    clock[0]=rotation.window_end+timedelta(seconds=1)
    rotation.scanner.scan_once(clock[0])
    report=rotation.step(clock[0])
    assert report['rotation']['current']==CID
    assert report['rotation']['phase']=='running'
    assert ex.sdk.cancel_order.call_count==0 and len(ex.posts)==2
    obs.plans[CID]=replace(obs.plans[CID],market=replace(obs.plans[CID].market,accepting_orders=False))
    clock[0]+=timedelta(seconds=10)
    report=rotation.step(clock[0])
    assert report['rotation']['phase']=='cleared' and report['rotation']['current'] is None
    assert report['rotation']['reason']=='needs_new_on'
    assert len(ex.posts)==2 and ex.sdk.create_limit_order.call_count==2
    assert {c.kwargs['order_id'] for c in ex.sdk.cancel_order.call_args_list}=={'order-1','order-2'}


def restart_rotation(rotation,ex,obs,clock):
    from lp_v2.journal import Journal
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.rotation_scan import ScanWorker
    from lp_v2.rotation_live import RotationRunner
    for runner in rotation._runners.values():runner.sdk.journal.close()
    rotation.journal.close()
    journal=Journal(__import__('pathlib').Path(rotation.config.state_dir)/'markets'/CID/'journal.sqlite')
    sdk=LiveSDK(ex.sdk,journal,owner='lp-v2:'+WALLET.lower(),condition_id=CID,token_ids=TOKENS,
                wallet=WALLET,signer=SIGNER,clock=lambda:clock[0])
    runner=LiveRunner(rotation.config,sdk,obs,clock=lambda:clock[0])
    scanner=ScanWorker(obs,clock=lambda:clock[0])
    return RotationRunner(rotation.config,runner,scanner,action_id=rotation.action_id,window_end=rotation.window_end,
        clock=lambda:clock[0],account_identity=rotation.account_identity,relayer={})


@pytest.mark.parametrize('phase',['running','exiting','cleared','revalidating','new_running'])
def test_restart_each_phase_without_replay(tmp_path,phase):
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(AT)
    original_preflight=obs.public_preflight
    if phase!='running':
        clock[0]=AT+timedelta(seconds=10);rotation.scanner.scan_once(clock[0])
        if phase=='exiting':ex.cancel_unknown=True
        if phase=='cleared':
            original_account=obs.read_account_readiness
            def expire(now,**kwargs):
                clock[0]=rotation.window_end
                return original_account(now,**kwargs)
            obs.read_account_readiness=expire
        if phase=='revalidating':
            def crash(now,cid):raise SystemExit('injected external read crash')
            obs.public_preflight=crash
            with pytest.raises(SystemExit):rotation.step(clock[0])
        else:rotation.step(clock[0])
    expected='running' if phase=='new_running' else phase
    assert rotation.phase==expected
    before=len(ex.posts)
    rotation=restart_rotation(rotation,ex,obs,clock)
    assert rotation.phase==expected
    assert rotation.snapshot()['rotation']['current']==(BID if phase=='new_running' else CID if phase in ('running','exiting') else None)
    obs.public_preflight=original_preflight
    ex.cancel_unknown=False
    clock[0]+=timedelta(seconds=10)
    rotation.scanner.scan_once(clock[0])
    rotation.step(clock[0])
    ids=[];merge_ids=[]
    for directory in (tmp_path/'markets').iterdir():
        from lp_v2.journal import Journal
        journal=Journal(directory/'journal.sqlite')
        ids += [p['request'].request_id for _,p in journal.events('intent')]
        merge_ids += [p['request'].merge_id for _,p in journal.events('merge_intent')]
        journal.close()
    assert len(ids)==len(set(ids))
    assert len(merge_ids)==len(set(merge_ids))
    if phase in ('running','exiting','revalidating'):
        assert len(ex.posts)==before+2 # First genuine B entry, never A replay.
    else:
        assert len(ex.posts)==before


def test_market_ids_and_single_risk_replay(tmp_path):
    from lp_v2.journal import Journal
    from lp_v2.models import Outcome, OrderStatus
    from lp_v2.rotation_state import open_market_journal
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(AT)
    cancel=ex.cancel
    def advance_cancel(**kwargs):
        result=cancel(**kwargs)
        clock[0]+=timedelta(milliseconds=1)
        return result
    ex.sdk.cancel_order.side_effect=advance_cancel
    for seconds,leader in ((10,BID),(21,CID),(32,BID)):
        obs.plans[leader]=replace(obs.plans[leader],market=replace(obs.plans[leader].market,rewards_daily_rate=1000+seconds))
        clock[0]=AT+timedelta(seconds=seconds)
        rotation.scanner.scan_once(clock[0]);rotation.step(clock[0])
        assert rotation.current.condition_id==leader
    histories=[];ids=[]
    for directory in (tmp_path/'markets').iterdir():
        journal=Journal(directory/'journal.sqlite')
        for seq,(kind,payload) in enumerate(journal.events()):
            if kind=='intent':
                ids.append(payload['request'].request_id)
                histories.append((payload['request'].created_at,directory.name,seq,kind,payload))
            if kind=='order_result':histories.append((payload.observed_at,directory.name,seq,kind,payload))
        journal.close()
    assert len(ids)==len(set(ids))==8
    assert all(f':{cid[:8]}:order:' in rid for cid in (CID,BID) for rid in ids if f':{cid[:8]}:' in rid)
    risk={}
    for _,cid,_,kind,payload in sorted(histories):
        if kind=='intent':risk[payload['request'].request_id]=cid
        elif payload.outcome==Outcome.FAILED or (payload.order and payload.order.authoritative and payload.order.status != OrderStatus.LIVE):
            risk.pop(payload.request.request_id,None)
        assert len(set(risk.values()))<=1
    # Fail closed for distinct full CIDs that collide at the mandated short prefix.
    collision='0x'+'a'*6+'d'*58
    journal=open_market_journal(tmp_path,dict(owner=rotation.sdk.owner,condition_id=collision,token_ids=('401','402')))
    journal.close()
    with pytest.raises(ValueError,match='prefix collision'):
        restart_rotation(rotation,ex,obs,clock)


from tests.test_live_control_profiles import config_home, known_ssh, profile
from tests.test_live_control_host import target, combined_world, operator_os


def control_rotation_boundary(world,monkeypatch):
    import json
    from pathlib import Path
    from tests.test_readonly_sdk import market_wire
    profile=world.profile
    old_cid=profile['trading']['market_condition_id']
    profile['trading']['market_condition_id']=CID
    env=Path(profile['files']['run_env']);env.write_text(env.read_text().replace(old_cid,CID))
    from lp_v2.live_control.profiles import config_root
    (config_root()/'profiles/tokyo.json').write_text(json.dumps(profile))
    world.http_overrides['/markets/keyset']=(200,dict(markets=[market_wire(CID)],next_cursor=None))
    external={}
    def launch():
        runtime,ex,obs,clock=rotation_world(Path(profile['files']['state_dir']),now=world.clock,
            identity=profile['identity'],code_sha=profile['release']['expected_sha'])
        external.update(ex=ex,obs=obs,clock=clock)
        return runtime
    world.runtime_factory=launch
    return external


def test_off_current_and_uncleared_markets_preserves_foreign_orders(combined_world,monkeypatch,capsys):
    import json
    from pathlib import Path
    from lp_v2.cli import main
    from tests.test_readonly_sdk import market_wire
    world=combined_world
    external=control_rotation_boundary(world,monkeypatch)
    monkeypatch.setattr('builtins.input',lambda _:'on')
    args=['live','on','--action-id','a'*32,'--window-end','2099-01-01T00:00:00+00:00']
    assert main(args)==0
    first=json.loads(capsys.readouterr().out.splitlines()[-1])
    runtime=world.runtime;ex=external['ex'];obs=external['obs'];clock=external['clock']
    world.clock+=timedelta(seconds=10);clock[0]=world.clock
    runtime.clock=lambda:world.clock;runtime.current.clock=lambda:world.clock
    runtime.scanner.scan_once(world.clock);runtime.step(world.clock)
    assert runtime.current.condition_id==BID
    assert main(args)==0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])==first
    ex.orders['manual']=ex.orders['order-3'].model_copy(update={'id':'manual'})
    assert main(['live','off','--action-id','b'*32])==0
    first_off=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert first_off['drain']=='PASS' and first_off['executor_stopped'] is True
    assert ex.orders['manual'].status=='LIVE'
    assert all(o.status!='LIVE' for oid,o in ex.orders.items() if oid!='manual')
    assert main(['live','off','--action-id','b'*32])==0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])==first_off
    assert world.mutations.count(('stop','lp-v2-live.service'))==1


def factory_boundary(tmp_path,monkeypatch):
    import json,httpx
    from pathlib import Path
    from types import SimpleNamespace
    from polymarket import SecureClient
    from polymarket.environments import PRODUCTION
    from tests.test_live_cli import live_args
    from tests.test_readonly_sdk import market_wire
    from tests.fakes.live_exchange import Exchange
    from lp_v2.cli import parse_run
    args,_=live_args(tmp_path)
    relay=tmp_path/'relayer.json'
    credentials=json.loads((tmp_path/'l2.json').read_text())
    relay.write_text(json.dumps(dict(key='synthetic-relayer',address=credentials['signer_address'])));relay.chmod(0o600)
    args+=['--relayer-credentials-file',str(relay),'--code-sha','a'*40]
    config=replace(parse_run(args),started_at=AT)
    state=Path(config.state_dir);state.mkdir(mode=0o700)
    ex=Exchange();current=[AT]
    token_cids={'101':CID,'102':CID,'201':BID,'202':BID}
    ex.sdk.wallet_type='POLY_PROXY';ex.sdk.wallet=credentials['wallet_address'];ex.sdk.signer=credentials['signer_address']
    object.__setattr__(ex.sdk,'_ctx',SimpleNamespace(rpc=None,relayer=None))
    sign,post=ex.sign,ex.post
    ex.sdk.create_limit_order.side_effect=lambda **kw:replace(sign(**kw),maker=ex.sdk.wallet,signer=ex.sdk.signer)
    def owned_post(signed):
        ex.condition_id=token_cids[signed.token_id]
        result=post(signed)
        if hasattr(result,'order_id'):
            ex.orders[result.order_id]=ex.orders[result.order_id].model_copy(update={'maker_address':ex.sdk.wallet})
        return result
    ex.sdk.post_order.side_effect=owned_post
    monkeypatch.setattr(SecureClient,'create',lambda **kw:ex.sdk)
    seen=[]
    def handle(transport,request):
        seen.append(request);assert request.method=='GET'
        path=request.url.path
        if path=='/markets/keyset':
            cid=request.url.params.get('condition_ids')
            wires=[]
            for market,tokens,reward in ((CID,['101','102'],'100'),(BID,['201','202'],'200')):
                if cid and cid!=market:continue
                wire=market_wire(market,clobTokenIds=tokens)
                wire['clobRewards'][0]['rewardsDailyRate']=reward
                wires.append(wire)
            data=dict(markets=wires,next_cursor=None)
        elif path=='/book':
            token=request.url.params['token_id']
            data=dict(market=token_cids[token],asset_id=token,timestamp=str(int(current[0].timestamp()*1000)),
                bids=[dict(price='.40',size='100')],asks=[dict(price='.42',size='100')],
                min_order_size='1',tick_size='.01',neg_risk=False,hash='fixture')
        elif path.startswith('/markets/'):data={'in_play':False}
        elif path=='/balance-allowance':
            data=dict(balance='0' if request.url.params.get('asset_type')=='CONDITIONAL' else '200000000',
                allowances={PRODUCTION.standard_exchange:'200000000',PRODUCTION.neg_risk_exchange:'200000000'})
        elif path=='/data/orders':data=dict(data=[],next_cursor='LTE=')
        elif path=='/auth/ban-status/closed-only':data=dict(closed_only=False)
        elif path=='/api/geoblock':data=dict(blocked=False,country='US')
        elif path=='/transactions':data=[]
        else:raise AssertionError(path)
        return httpx.Response(200,json=data,request=request)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',handle)
    identity={k:credentials[k] for k in ('wallet_address','signer_address','signature_type')}
    profile=dict(identity=identity,trading=dict(market_condition_id=CID,capital_usdc='100'),
        release=dict(expected_sha=config.code_sha),files=dict(state_dir=config.state_dir,
        signing_key=str(tmp_path/'signing'),l2_credentials=str(tmp_path/'l2.json'),relayer_credentials=str(relay)))
    record=dict(action_id='a'*32,window_end=(AT+timedelta(minutes=5)).isoformat(),profile=profile,
                run_id='b'*32,started_at=AT.isoformat(),journal_seq=0)
    from lp_v2.live_control.profiles import atomic_json
    atomic_json(state/'live-control-run.json',record)
    atomic_json(state/'process.json',dict(run_id='b'*32,pid=1234,identity='fake-boot:123'))
    return config,ex,current,record,seen


def confirm_factory_seed(runner,record,current,monkeypatch):
    """Real seed decisions plus the host's proof writer; only process/UTC facts are fake."""
    import datetime as datetime_module
    from datetime import datetime as RealDateTime
    from lp_v2.live_control.host import record_start_observation
    request=dict(profile=record['profile'],run_id=record['run_id'],action_id=record['action_id'])
    for expected in (1,2):
        current[0]+=timedelta(seconds=10)
        runner.step(current[0])
        snapshot=dict(run_id=record['run_id'],executor_running=True,readonly_stopped=True,
                      identity=record['profile']['identity'],summary=runner.snapshot())
        class Clock(RealDateTime):
            @classmethod
            def now(cls,tz=None):return current[0]
        with monkeypatch.context() as temporal:
            temporal.setattr(datetime_module,'datetime',Clock)
            result=record_start_observation(request,snapshot)
        assert result['verified_rounds']==expected


@pytest.mark.parametrize('case',['normal','no_control','missing_window','wrong_owner','wrong_token','traversal','symlink','legacy'])
def test_production_factory_control_window_and_market_paths(tmp_path,monkeypatch,case):
    from pathlib import Path
    import json
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.rotation_live import RotationRunner
    from lp_v2.journal import Journal
    from lp_v2.live_control.profiles import atomic_json
    config,ex,current,record,seen=factory_boundary(tmp_path,monkeypatch)
    state=Path(config.state_dir)
    if case=='no_control':(state/'live-control-run.json').unlink()
    if case=='missing_window':
        record.pop('window_end');atomic_json(state/'live-control-run.json',record)
        with pytest.raises(ValueError):create_live_runner(config,clock=lambda:current[0])
        assert ex.sdk.create_limit_order.call_count==0
        return
    prefix=None
    if case=='legacy':
        journal=Journal(state/'journal.sqlite')
        journal.append('identity',dict(owner='lp-v2:'+config.credentials.wallet_address.lower(),condition_id=CID,token_ids=TOKENS))
        journal.append('diagnostic',dict(reason='retained legacy history',at=AT))
        prefix=journal.events();journal.close();(state/'journal.sqlite').chmod(0o600)
        original=(state/'journal.sqlite').read_bytes()
    runner=create_live_runner(config,clock=lambda:current[0])
    assert isinstance(runner,RotationRunner)
    assert Path(runner.sdk.journal.path)==state/'markets'/CID/'journal.sqlite'
    if case=='no_control':
        assert runner.window_end is None and runner.action_id is None
        runner.step(AT)
        current[0]+=timedelta(seconds=10);runner.scanner.scan_once(current[0]);runner.step(current[0])
        assert runner.current.condition_id==CID and len(ex.posts)==2
        runner.close();return
    assert runner.action_id==record['action_id'] and runner.window_end==AT+timedelta(minutes=5)
    runner.step(AT)
    confirm_factory_seed(runner,record,current,monkeypatch)
    current[0]+=timedelta(seconds=10);runner.scanner.scan_once(current[0]);runner.step(current[0])
    assert runner.current.condition_id==BID
    assert runner.scanner.sdk is not runner.observations
    assert runner.sdk.owner=='lp-v2:'+config.credentials.wallet_address.lower()
    if case=='legacy':
        journal=Journal(state/'journal.sqlite');assert journal.events()==prefix;journal.close()
        assert (state/'journal.sqlite').read_bytes()==original
        journal=Journal(state/'markets'/CID/'journal.sqlite');assert journal.events()[:len(prefix)]==prefix;journal.close()
    runner.close()
    if case in ('wrong_owner','wrong_token'):
        journal=Journal(state/'markets'/BID/'journal.sqlite')
        identity=journal.events('identity')[0][1]
        changed=dict(identity,owner='lp-v2:foreign') if case=='wrong_owner' else dict(identity,token_ids=('999','998'))
        journal.append('identity',changed);journal.close()
    elif case=='traversal':
        journal=Journal(state/'rotation.sqlite');last=journal.events('rotation')[-1][1]
        journal.append('rotation',dict(last,new_market='../escape'));journal.close()
    elif case=='symlink':
        market=state/'markets'/BID
        moved=state/'retained-market';market.rename(moved);market.symlink_to(moved,target_is_directory=True)
    if case in ('wrong_owner','wrong_token','traversal','symlink'):
        with pytest.raises(ValueError):create_live_runner(config,clock=lambda:current[0])
    else:
        restored=create_live_runner(config,clock=lambda:current[0])
        assert restored.current.condition_id==BID
        assert restored.snapshot()['rotation']['current']==BID
        if case=='normal':assert restored.drain(timeout_s=0)==0
        restored.close()
        if case=='normal':
            current[0]=AT+timedelta(seconds=60)
            new_record=dict(record,action_id='c'*32,run_id='d'*32,started_at=current[0].isoformat())
            atomic_json(state/'live-control-run.json',new_record)
            atomic_json(state/'process.json',dict(run_id='d'*32,pid=1234,identity='fake-boot:123'))
            next_on=create_live_runner(replace(config,started_at=current[0]),clock=lambda:current[0])
            assert next_on.current.condition_id==CID
            next_on.step(current[0])
            assert tuple(s.token_id for s in ex.posts[-2:])==TOKENS
            next_on.close()
    assert all(r.method=='GET' for r in seen)


def test_estimate_never_changes_ranking_rotation_or_orders(tmp_path):
    class ExplodingEstimate:
        def __float__(self):raise RuntimeError('display estimate unavailable')
        def __call__(self):raise RuntimeError('display estimate unavailable')
    results=[]
    for index,estimate in enumerate((D('5'),D('0'),'UNKNOWN',ExplodingEstimate())):
        path=tmp_path/str(index);path.mkdir()
        rotation,ex,obs,clock=rotation_world(path)
        source=obs.scan_markets
        def scan(now):
            result=source(now)
            return replace(result,rows=tuple(dict(row,estimated_lp_daily=estimate) for row in result.rows))
        obs.scan_markets=scan
        rotation.step(AT)
        clock[0]=AT+timedelta(seconds=10);rotation.scanner.scan_once(clock[0]);report=rotation.step(clock[0])
        results.append((report['rotation']['current'],report['rotation']['phase'],
                        tuple(s.token_id for s in ex.posts),tuple(p['phase'] for _,p in rotation.journal.events('rotation'))))
        rotation.close()
    assert results==[(BID,'running',('101','102','201','202'),('running','exiting','cleared','revalidating','running'))]*4


@pytest.mark.parametrize('boundary',['clearance','drain'])
def test_late_fill_during_clearance_or_drain_keeps_recovery_running(tmp_path,boundary):
    from types import SimpleNamespace
    rotation,ex,obs,clock=rotation_world(tmp_path)
    rotation.step(AT)
    inventory_seen=[False];after_inventory=[0];injected=[False]
    base_inventory=obs.read_conditional_inventory
    def inventory(*args):
        result=base_inventory(*args)
        inventory_seen[0]=True
        return result
    obs.read_conditional_inventory=inventory
    def opens(**kwargs):
        if inventory_seen[0]:
            after_inventory[0]+=1
            if not injected[0] and after_inventory[0]==(1 if boundary=='clearance' else 2):
                injected[0]=True;ex.condition_id=CID
                ex.fill('order-1','4','.40',at=AT+timedelta(seconds=10))
        return (SimpleNamespace(items=tuple(o for o in ex.orders.values() if o.status=='LIVE')),)
    ex.sdk.list_open_orders.side_effect=opens
    clock[0]=AT+timedelta(seconds=10);rotation.scanner.scan_once(clock[0]);report=rotation.step(clock[0])
    assert injected[0]
    assert report['rotation']['current']==CID and report['rotation']['phase']=='exiting'
    assert not rotation.current.stop.is_set()
    assert {s.token_id for s in ex.posts}<=set(TOKENS)
    clock[0]+=timedelta(seconds=10)
    report=rotation.step(clock[0])
    assert report['rotation']['phase']=='exiting'
    hedges=[r for r in rotation.current.ledger.snapshot().requests if r.purpose=='hedge']
    assert len(hedges)==1 and hedges[0].quote.quantity==D('4')
    assert hedges[0].quote.price<=D('.58') and hedges[0].quote.post_only
    assert {s.token_id for s in ex.posts}<=set(TOKENS)
    assert len(rotation.current.ledger.snapshot().fills)==1


@pytest.mark.parametrize('problem',['nonzero','unknown','token_mismatch','missing_public','missing_account','all_fail','retained_flat'])
def test_target_inventory_and_missing_preflight_facts_skip_candidate(tmp_path,problem):
    from lp_v2.rotation_state import open_market_journal
    rotation,ex,obs,clock=rotation_world(tmp_path)
    obs.plans[CID_C]=replace(obs.plans[CID_C],market=replace(obs.plans[CID_C].market,rewards_daily_rate=150))
    obs.ranked=(CID,BID,CID_C)
    if problem=='retained_flat':
        journal=open_market_journal(tmp_path,dict(owner=rotation.sdk.owner,condition_id=BID,token_ids=('201','202')))
        journal.close()
    if problem in ('nonzero','all_fail'):ex.quantities['201']=D('2')
    if problem=='all_fail':ex.quantities['301']=D('1');obs.fail_public={CID}
    original_inventory=obs.read_conditional_inventory
    def inventory(now,cid):
        if cid==BID and problem=='unknown':raise ValueError('inventory unavailable')
        row=original_inventory(now,cid)
        if cid==BID and problem=='token_mismatch':row['token_ids']=('999','998')
        return row
    obs.read_conditional_inventory=inventory
    original_public=obs.public_preflight;original_account=obs.read_account_readiness
    def public(now,cid):
        result=original_public(now,cid)
        if cid==BID and problem=='missing_public':result.pop('token_identity')
        return result
    obs.public_preflight=public
    seen_candidate=[None]
    original_books=obs.read_fixed_market
    def books(now,cid):
        seen_candidate[0]=cid
        return original_books(now,cid)
    obs.read_fixed_market=books
    def account(now,**kwargs):
        result=original_account(now,**kwargs)
        if seen_candidate[0]==BID and problem=='missing_account':result.pop('allowance')
        return result
    obs.read_account_readiness=account
    rotation.step(AT)
    clock[0]+=timedelta(seconds=10);rotation.scanner.scan_once(clock[0]);report=rotation.step(clock[0])
    expected=BID if problem=='retained_flat' else None if problem=='all_fail' else CID_C
    assert report['rotation']['current']==expected
    assert not any(s.token_id in ('201','202') for s in ex.posts) if problem!='retained_flat' else len(ex.posts)==4
    if expected is None:
        assert len(ex.posts)==2 and report['rotation']['phase']=='cleared'
    elif problem!='retained_flat':
        assert tuple(s.token_id for s in ex.posts[2:])==('301','302')
    rotation.close()


def test_rotated_stopped_handoff_and_next_on_validate_active_journal(combined_world,monkeypatch,capsys):
    import json,httpx
    from pathlib import Path
    from lp_v2.cli import main
    from lp_v2.live_control.host import account_checks
    from lp_v2.live_control.workflow import valid_round
    from lp_v2.live_control.profiles import atomic_json
    from tests.test_readonly_sdk import market_wire
    world=combined_world;external=control_rotation_boundary(world,monkeypatch)
    profile=world.profile
    monkeypatch.setattr('builtins.input',lambda _:'on')
    args=['live','on','--action-id','a'*32,'--window-end','2099-01-01T00:00:00+00:00']
    assert main(args)==0;capsys.readouterr()
    runtime=world.runtime
    world.clock+=timedelta(seconds=10);external['clock'][0]=world.clock
    runtime.scanner.scan_once(world.clock);runtime.step(world.clock)
    assert runtime.current.condition_id==BID
    started=json.loads((Path(profile['files']['state_dir'])/'live-control-run.json').read_text())
    snapshot=dict(run_id=world.lease.record['run_id'],executor_running=True,readonly_stopped=True,
                  identity=profile['identity'],summary=runtime.snapshot())
    assert valid_round(profile,snapshot['run_id'],snapshot,started['started_at']) is None
    assert main(['live','off','--action-id','b'*32])==0;capsys.readouterr()
    route=httpx.HTTPTransport.handle_request;variant=[None]
    def http(transport,request):
        if request.url.path=='/markets/keyset' and request.url.params.get('condition_ids')==BID:
            cid=CID if variant[0]=='market' else BID
            tokens=['901','902'] if variant[0]=='tokens' else ['201','202']
            return httpx.Response(200,json=dict(markets=[market_wire(cid,clobTokenIds=tokens)],next_cursor=None),request=request)
        if request.url.path=='/balance-allowance' and request.url.params.get('asset_type')=='CONDITIONAL' and request.url.params.get('token_id') in ('201','202','901','902'):
            return httpx.Response(200,json=dict(balance='0',allowances={}),request=request)
        return route(transport,request)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',http)
    state=Path(profile['files']['state_dir'])
    control=json.loads((state/'live-control-run.json').read_text())
    atomic_json(state/'live-control-run.json',dict(control,journal_seq=999,journal_seqs={CID:999,BID:0}))
    assert account_checks(profile)['inventory_handoff']['status']=='PASS'
    process=json.loads((state/'process.json').read_text())
    final=json.loads((state/'stop-result.json').read_text())
    atomic_json(state/'process.json',dict(process,lease_id='1'*32))
    atomic_json(state/'stop-result.json',dict(final,lease_id='2'*32))
    assert account_checks(profile)['inventory_handoff']['status']!='PASS'
    atomic_json(state/'stop-result.json',dict(final,lease_id='1'*32))
    assert account_checks(profile)['inventory_handoff']['status']=='PASS'
    atomic_json(state/'process.json',process);atomic_json(state/'stop-result.json',final)
    for bad in ('market','tokens','run'):
        variant[0]=bad
        if bad=='run':atomic_json(state/'process.json',dict(process,run_id='e'*32))
        assert account_checks(profile)['inventory_handoff']['status']!='PASS'
    atomic_json(state/'process.json',process);variant[0]=None
    credentials=Path(profile['files']['l2_credentials']);original=credentials.read_bytes()
    payload=json.loads(original);payload['wallet_address']='0x'+'9'*40
    credentials.write_text(json.dumps(payload))
    assert account_checks(profile)['private_identity']['status']=='FAIL'
    credentials.write_bytes(original)
    assert main(['live','check'])==0;capsys.readouterr()


def test_restart_drains_retained_owned_history(tmp_path):
    from lp_v2.rotation_state import open_market_journal
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.live_runtime import LiveRunner
    rotation,ex,obs,clock=rotation_world(tmp_path)
    identity=dict(owner=rotation.sdk.owner,condition_id=CID_C,token_ids=('301','302'))
    journal=open_market_journal(tmp_path,identity)
    sdk=LiveSDK(ex.sdk,journal,**identity,wallet=WALLET,signer=SIGNER,clock=lambda:clock[0])
    historical=LiveRunner(rotation.config,sdk,obs,clock=lambda:clock[0])
    historical.step(AT)
    assert tuple(s.token_id for s in ex.posts)==('301','302')
    ex.orders['foreign']=ex.orders['order-1'].model_copy(update={'id':'foreign'})
    journal.close()
    rotation=restart_rotation(rotation,ex,obs,clock)
    clock[0]+=timedelta(seconds=10)
    report=rotation.step(clock[0])
    assert len(ex.posts)==2
    assert report['rotation']['reason'].startswith('historical_')
    assert rotation.drain(timeout_s=0)==0
    assert ex.orders['foreign'].status=='LIVE'
    assert ex.orders['order-1'].status==ex.orders['order-2'].status=='CANCELED'
    assert rotation.snapshot()['rotation']['cancel_count']==2
    rotation.close()


def test_fast_scan_cannot_rotate_before_seed_start_verification(tmp_path,monkeypatch):
    import copy,json
    from pathlib import Path
    from datetime import datetime as RealDateTime
    import datetime as datetime_module
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.live_control.profiles import atomic_json
    from lp_v2.journal import Journal
    config,ex,current,record,seen=factory_boundary(tmp_path,monkeypatch)
    class Clock(RealDateTime):
        @classmethod
        def now(cls,tz=None):return current[0]
    current[0]=Clock.fromisoformat(current[0].isoformat())
    monkeypatch.setattr(datetime_module,'datetime',Clock)
    runner=create_live_runner(config,clock=lambda:current[0])
    runner.scanner.scan_once(current[0]) # Catalog B wins before the first decision.
    runner.step(current[0])
    assert runner.current.condition_id==CID
    assert tuple(s.token_id for s in ex.posts)==TOKENS
    from lp_v2.live_control.host import record_start_observation
    request=dict(profile=record['profile'],run_id=record['run_id'],action_id=record['action_id'])
    def observation(runtime):
        return dict(run_id=record['run_id'],executor_running=True,readonly_stopped=True,
                    identity=record['profile']['identity'],summary=runtime.snapshot())
    current[0]+=timedelta(seconds=10);runner.step(current[0])
    assert record_start_observation(request,observation(runner))['verified_rounds']==1
    assert record_start_observation(request,observation(runner))['verified_rounds']==1
    runner.close()
    runner=create_live_runner(config,clock=lambda:current[0])
    runner.scanner.scan_once(current[0])
    current[0]+=timedelta(seconds=10);runner.step(current[0])
    assert runner.current.condition_id==CID and len(ex.posts)==2
    result=record_start_observation(request,observation(runner))
    assert result['status']=='PASS' and result['verified_rounds']==2
    path=Path(config.state_dir)/'live-control-run.json'
    verified=json.loads(path.read_text())
    for field in ('action_id','run_id'):
        corrupt=copy.deepcopy(verified);corrupt['startup_verified'][field]='f'*32
        atomic_json(path,corrupt)
        current[0]+=timedelta(seconds=10);runner.step(current[0])
        assert runner.current.condition_id==CID and len(ex.posts)==2
    atomic_json(path,verified)
    current[0]+=timedelta(seconds=10);report=runner.step(current[0])
    assert report['rotation']['current']==BID
    assert tuple(s.token_id for s in ex.posts)==('101','102','201','202')
    before=tuple(p['request_id'] for _,p in runner.sdk.journal.events('signed'))
    runner.close()
    runner=create_live_runner(config,clock=lambda:current[0])
    current[0]+=timedelta(seconds=10);runner.step(current[0])
    assert runner.current.condition_id==BID and len(ex.posts)==4
    assert tuple(p['request_id'] for _,p in runner.sdk.journal.events('signed'))==before
    runner.close()


@pytest.mark.parametrize('phase',['running','exiting','cleared','revalidating','new_running'])
@pytest.mark.parametrize('expired',[False,True])
def test_process_restart_resumes_same_on_without_replay(tmp_path,monkeypatch,phase,expired):
    import copy,json,httpx
    from pathlib import Path
    from lp_v2.live_process import ProcessLock
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.live_control.profiles import atomic_json
    from lp_v2.journal import Journal
    config,ex,current,record,seen=factory_boundary(tmp_path,monkeypatch)
    state=Path(config.state_dir)
    identity=['boot:first']
    monkeypatch.setattr('lp_v2.live_process.process_identity',lambda pid:identity[0])
    atomic_json(state/'live-control-run.json',dict(record,run_id=None))
    route=httpx.HTTPTransport.handle_request
    with ProcessLock(state) as original:
        runner=create_live_runner(config,clock=lambda:current[0])
        saved=json.loads((state/'live-control-run.json').read_text())
        saved['run_id']=original.record['run_id'];atomic_json(state/'live-control-run.json',saved)
        runner.step(current[0])
        confirm_factory_seed(runner,saved,current,monkeypatch)
        if phase!='running':
            current[0]+=timedelta(seconds=10);runner.scanner.scan_once(current[0])
            if phase=='exiting':ex.cancel_unknown=True
            def fail_entry(transport,request):
                if request.url.path=='/transactions':
                    if phase=='revalidating':raise SystemExit('external read crash')
                    if phase=='cleared':return httpx.Response(401,json={},request=request)
                return route(transport,request)
            with monkeypatch.context() as boundary:
                boundary.setattr(httpx.HTTPTransport,'handle_request',fail_entry)
                if phase=='revalidating':
                    with pytest.raises(SystemExit):runner.step(current[0])
                else:runner.step(current[0])
        assert runner.phase==('running' if phase=='new_running' else phase)
        before=len(ex.posts);old_physical=dict(original.record)
        runner.close() # Crash boundary: no drain or finish, but saved journals remain.
    identity[0]='boot:restarted'
    current[0]=AT+timedelta(seconds=310 if expired else 60)
    ex.cancel_unknown=False
    with ProcessLock(state) as replacement:
        recovered=create_live_runner(replace(config,started_at=current[0]),clock=lambda:current[0])
        assert replacement.record['identity']!=old_physical['identity']
        assert replacement.record['lease_id']!=old_physical['lease_id']
        assert replacement.record['run_id']==old_physical['run_id']
        with pytest.raises(BlockingIOError):
            with ProcessLock(state):pass
        recovered.scanner.scan_once(current[0]);report=recovered.step(current[0])
        if expired:
            assert len(ex.posts)==before
            expected=BID if phase=='new_running' else CID if phase=='running' else None
            assert report['rotation']['current']==expected
        else:
            assert report['rotation']['current']==BID
            assert len(ex.posts)==(before if phase=='new_running' else before+2)
        if phase=='running' and not expired:
            import os
            import lp_v2.live_process as process_module
            ticks=[0];signals=[]
            atomic_json(state/'stop-result.json',dict(run_id=replacement.record['run_id'],lease_id=old_physical['lease_id'],exit_code=0))
            actual_close=os.close
            with monkeypatch.context() as operating_system:
                operating_system.setattr(os,'pidfd_open',lambda *args:777,raising=False)
                operating_system.setattr(process_module.signal,'pidfd_send_signal',lambda *args:signals.append(args),raising=False)
                operating_system.setattr(os,'close',lambda fd:None if fd==777 else actual_close(fd))
                def wait(seconds):
                    ticks[0]+=seconds
                    assert recovered.drain(timeout_s=0)==0
                    replacement.finish(0)
                assert process_module.stop_process(state,timeout_s=2,monotonic=lambda:ticks[0],wait=wait)==0
            assert len(signals)==1 and ticks[0]==1 # Old physical stop evidence cannot satisfy this stop.
        recovered.close()
        if phase=='new_running' and not expired:
            bound=json.loads((state/'live-control-run.json').read_text())
            process=json.loads((state/'process.json').read_text())
            for problem in ('owner','action','config','process'):
                bad=copy.deepcopy(bound);bad_process=dict(process)
                if problem=='owner':bad['profile']['identity']['wallet_address']='0x'+'9'*40
                if problem=='action':bad['action_id']='f'*32
                if problem=='config':bad['profile']['release']['expected_sha']='f'*40
                if problem=='process':bad_process['identity']='stale-foreign-process'
                atomic_json(state/'live-control-run.json',bad);atomic_json(state/'process.json',bad_process)
                with pytest.raises(ValueError):create_live_runner(replace(config,started_at=current[0]),clock=lambda:current[0])
                atomic_json(state/'live-control-run.json',bound);atomic_json(state/'process.json',process)
            journal=Journal(state/'markets'/BID/'journal.sqlite');known=journal.events('identity')[0][1]
            journal.append('identity',dict(known,token_ids=('999','998')));journal.close()
            with pytest.raises(ValueError):create_live_runner(replace(config,started_at=current[0]),clock=lambda:current[0])
        identifiers=[]
        for market in (state/'markets').iterdir():
            journal=Journal(market/'journal.sqlite')
            identifiers += [p['request'].request_id for _,p in journal.events('intent')]
            journal.close()
        assert len(identifiers)==len(set(identifiers))
