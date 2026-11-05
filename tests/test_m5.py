"""M5 public-boundary regressions; expected money and identities are independent literals."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess

import pytest

from lp_v2.cli import RunConfig, parse_run
from tests.test_live_cli import live_args, historical_market_clock
from tests.test_readonly_sdk import step_at
from tests.test_live_sdk import AT, CID, TOKENS, OWNER, WALLET, SIGNER

CIDS = (CID, '0x'+'b'*64, '0x'+'c'*64)
PAIRS = (TOKENS, ('201','202'), ('301','302'))


def test_markets_cli_and_launcher(tmp_path):
    args, _ = live_args(tmp_path)
    assert parse_run(args).market == CID
    for n in (1,2,3):
        extra = [v for cid in CIDS[1:n] for v in ('--market',cid)]
        config = parse_run(args+extra+['--market',CID])
        assert config.markets == CIDS[:n]
        assert config.market == CID
    for cid in ('', 'alien', '0x'+'f'*63):
        bad=args.copy();bad[bad.index('--market')+1]=cid
        with pytest.raises(SystemExit):parse_run(bad)
    with pytest.raises(SystemExit):parse_run(args+['--market',CIDS[1],'--market',CIDS[2],'--market','0x'+'d'*64])
    with pytest.raises(SystemExit):parse_run(['run','--capital-usdc','100','--market',CID])
    release=tmp_path/'release';(release/'deploy').mkdir(parents=True);(release/'.venv/bin').mkdir(parents=True)
    launcher=release/'deploy/run-live';launcher.write_bytes(Path('deploy/run-live').read_bytes())
    fake=release/'.venv/bin/lp-v2';fake.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n');fake.chmod(0o700)
    env=dict(os.environ,LP_V2_CAPITAL_USDC='100',LP_V2_CODE_SHA='a'*40,LP_V2_MARKETS=','.join(CIDS))
    result=subprocess.run(['bash',str(launcher)],env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    argv=json.loads(result.stdout)
    assert [argv[i+1] for i,v in enumerate(argv) if v=='--market']==list(CIDS)
    assert argv[:4]==['run','--live','--capital-usdc','100']
    for value in ('', CID+',', ','+CID, CID+',,'+CIDS[1]):
        env['LP_V2_MARKETS']=value
        assert subprocess.run(['bash',str(launcher)],env=env,capture_output=True).returncode != 0


def multi_world(tmp_path):
    from lp_v2.journal import Journal
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.live_runtime import LiveRunner
    from tests.fakes.live_exchange import Exchange
    from tests.test_live_runtime import inputs, Observations
    from lp_v2.models import Token
    markets=dict(zip(CIDS,PAIRS))
    ex=Exchange(markets=markets)
    journal=Journal(tmp_path/'journal.sqlite')
    sdk=LiveSDK(ex.sdk,journal,owner=OWNER,condition_id=CID,token_ids=TOKENS,markets=markets,wallet=WALLET,signer=SIGNER,clock=lambda:ex.now)
    plans=[]
    for cid,tokens in zip(CIDS,PAIRS):
        p=inputs()
        plans.append(replace(p,market=replace(p.market,condition_id=cid,tokens=tuple(Token(t,n) for t,n in zip(tokens,('Yes','No')))),books=tuple(replace(b,condition_id=cid,token_id=t) for b,t in zip(p.books,tokens))))
    obs=Observations(tuple(plans))
    runner=LiveRunner(RunConfig(D('100'),AT,market=CID,markets=CIDS),sdk,obs,clock=lambda:ex.now)
    return runner,ex,sdk,journal,obs


def test_unchanged_order_readbacks_do_not_grow_journal(tmp_path):
    from lp_v2.models import OrderStatus
    runner,ex,sdk,journal,obs=multi_world(tmp_path)
    runner.step(AT)
    requests=tuple(r.request for _,r in journal.events('order_result'))
    assert len(requests)==6
    for tick in range(1,22):
        ex.now=AT+timedelta(seconds=10*tick)
        obs.plans=tuple(replace(p,books=tuple(replace(b,observed_at=ex.now) for b in p.books)) for p in obs.plans)
        runner.step(ex.now)
        facts=sdk.read_orders()
        assert len(facts)==6 and all(o.authoritative and o.observed_at==ex.now for o in facts)
        assert len(journal.events('order_result'))==12
    assert len(ex.posts)==6 and ex.sdk.cancel_order.call_count==0
    assert {o.request for o in facts}==set(requests)
    assert all(r.created_at==AT and r.expires_at==AT+timedelta(hours=1) for r in requests)
    oid='order-1'
    ex.now+=timedelta(seconds=10)
    ex.fill(oid,'1','.40',at=ex.now)
    partial=next(o for o in sdk.read_orders() if o.order_id==oid)
    assert partial.filled_quantity==D('1') and partial.observed_at==ex.now
    assert journal.events('order_result')[-1][1].order==partial
    ex.now+=timedelta(seconds=10)
    ex.orders[oid]=ex.orders[oid].model_copy(update={'status':'DELAYED'})
    uncertain=next(o for o in sdk.read_orders() if o.order_id==oid)
    assert not uncertain.authoritative and uncertain.observed_at==ex.now
    assert journal.events('order_result')[-1][1].order==uncertain
    ex.now+=timedelta(seconds=10)
    ex.orders[oid]=ex.orders[oid].model_copy(update={'status':'LIVE'})
    recovered=next(o for o in sdk.read_orders() if o.order_id==oid)
    assert recovered.authoritative and recovered.observed_at==ex.now
    assert journal.events('order_result')[-1][1].order==recovered
    ex.now+=timedelta(seconds=10)
    cancelled_at=ex.now
    sdk.cancel_order(oid,at=cancelled_at)
    ex.now+=timedelta(seconds=10)
    terminal=next(o for o in sdk.read_orders() if o.order_id==oid)
    assert terminal.status==OrderStatus.CANCELLED and terminal.authoritative
    assert terminal.cancel_requested_at==cancelled_at and terminal.observed_at==ex.now
    assert terminal.filled_quantity==D('1') and terminal.remaining_quantity==0
    assert journal.events('order_result')[-1][1].order==terminal
    count=len(journal.events('order_result'))
    ex.now+=timedelta(seconds=10)
    assert next(o for o in sdk.read_orders() if o.order_id==oid)==terminal
    assert len(journal.events('order_result'))==count and len(ex.posts)==6
    journal.close()


def test_fixed_market_failure_is_isolated(tmp_path):
    import httpx
    from tests.test_live_runtime import Observations
    from tests.test_live_income import earning
    runner,ex,sdk,journal,obs=multi_world(tmp_path)
    class FixedObservations(Observations):
        failed=False
        def read_fixed_market(self,now,condition_id):
            if self.failed and condition_id==CIDS[2]:
                raise ValueError('market excluded')
            return [r for r in self.read_markets(now) if r['condition_id']==condition_id]
    obs=FixedObservations(obs.plans)
    runner.observations=obs
    ex.sdk.list_user_earnings_for_day.return_value=(SimpleNamespace(items=tuple(earning(cid=cid) for cid in CIDS)),)
    ex.sdk.get_reward_percentages.return_value={cid:25 for cid in CIDS}
    ex.sdk.get_order_scoring.return_value=True
    sdk.http=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=[])))
    runner.step(AT)
    assert len(ex.posts)==6
    obs.failed=True
    for tick in range(1,4):
        ex.now=AT+timedelta(seconds=10*tick)
        obs.plans=tuple(replace(p,books=tuple(replace(b,observed_at=ex.now) for b in p.books)) for p in obs.plans)
        result=runner.step(ex.now)
        active={cid:[o for o in ex.orders.values() if o.condition_id==cid and o.status=='LIVE'] for cid in CIDS}
        assert [len(active[cid]) for cid in CIDS]==[2,2,0]
        assert {c.kwargs['order_id'] for c in ex.sdk.cancel_order.call_args_list}=={'order-5','order-6'}
        assert len(ex.posts)==6
        rows={r['condition_id']:r for r in result['markets']}
        assert not rows[CIDS[2]]['eligible'] and rows[CIDS[2]]['skip_reason']
        for cid in CIDS[:2]:
            assert rows[cid]['eligible']
            assert all(D(rows[cid]['m4'][k]).is_finite() for k in ('reward_today','rebate_today','net_profit','lp_yield_pct_per_day','net_yield_pct_per_day'))
        assert set(journal.events('occupation')[-1][1]['markets'])==set(CIDS)
    obs.failed=False
    ex.now+=timedelta(seconds=10)
    obs.plans=tuple(replace(p,books=tuple(replace(b,observed_at=ex.now) for b in p.books)) for p in obs.plans)
    recovered=runner.step(ex.now)
    assert len(ex.posts)==8 and all(r['eligible'] for r in recovered['markets'])
    assert [sum(o.status=='LIVE' and o.condition_id==cid for o in ex.orders.values()) for cid in CIDS]==[2,2,2]
    sdk.http.close();journal.close()


@pytest.mark.parametrize('stale_tokens', [TOKENS, TOKENS[:1], TOKENS[1:]],
                         ids=['both-books', 'yes-book', 'no-book'])
def test_multimarket_book_age_at_completed_is_isolated(tmp_path, stale_tokens):
    import httpx
    from polymarket import PublicClient
    from polymarket.clients._transport import SyncTransport
    from lp_v2.journal import Journal
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.readonly_sdk import ReadOnlySDK
    from tests.fakes.live_exchange import Exchange
    from tests.test_readonly_sdk import market_wire

    markets = dict(zip(CIDS[:2], PAIRS[:2]))
    ex = Exchange(markets=markets)
    start, delay = AT, 3
    seen = []

    def http(req):
        seen.append(req.method)
        assert req.method == 'GET'
        if req.url.path == '/markets/keyset':
            cid = req.url.params['condition_ids']
            return httpx.Response(200, json={'markets': [market_wire(cid, clobTokenIds=list(markets[cid]))], 'next_cursor': None})
        if req.url.path == '/book':
            token = req.url.params['token_id']
            cid = next(cid for cid, pair in markets.items() if token in pair)
            ex.now = start + timedelta(seconds=0 if cid == CID else delay)
            stamp = start - timedelta(seconds=58) if token in stale_tokens else ex.now
            bid, ask = ('0.46', '0.48') if token == markets[cid][0] else ('0.51', '0.53')
            return httpx.Response(200, json=dict(market=cid, asset_id=token, timestamp=str(int(stamp.timestamp()*1000)),
                bids=[dict(price=bid, size='100')], asks=[dict(price=ask, size='100')],
                min_order_size='1', tick_size='0.01', neg_risk=False, hash='fixture'))
        if req.url.path.startswith('/markets/'):
            return httpx.Response(200, json={'in_play': False})
        if req.url.path == '/api/geoblock':
            return httpx.Response(200, json=dict(blocked=False, country='US'))
        raise AssertionError(str(req.url))

    public = PublicClient()
    for name in ('gamma', 'clob'):
        getattr(public._ctx, name).close()
        url = 'https://' + name + '.polymarket.com'
        public._ctx = replace(public._ctx, **{name: SyncTransport(base_url=url,
            client=httpx.Client(base_url=url, transport=httpx.MockTransport(http)))})
    observations = ReadOnlySDK(public=public, http=httpx.Client(transport=httpx.MockTransport(http)), clock=lambda: ex.now)
    journal = Journal(tmp_path/'journal.sqlite')
    sdk = LiveSDK(ex.sdk, journal, owner=OWNER, condition_id=CID, token_ids=TOKENS,
        markets=markets, wallet=WALLET, signer=SIGNER, clock=lambda: ex.now)
    runner = LiveRunner(RunConfig(D('100'), AT, market=CID, markets=CIDS[:2]), sdk, observations, clock=lambda: ex.now)
    try:
        # A first becomes eligible on the existing ten-minute ranking refresh.
        for tick, delay, active_a, total_signed in ((0, 3, 0, 2), (610, 2, 2, 4), (620, 3, 0, 4), (630, 2, 2, 6)):
            start = ex.now = AT + timedelta(seconds=tick)
            report = runner.step(start)
            requests = runner.ledger.snapshot().requests
            assert sum(r.quote.condition_id == CID for r in requests) == total_signed - 2
            assert sum(r.quote.condition_id == CIDS[1] for r in requests) == 2
            assert len(journal.events('signed')) == len(ex.posts) == total_signed
            assert [sum(o.condition_id == cid and o.status == 'LIVE' for o in ex.orders.values())
                    for cid in markets] == [active_a, 2]
            rows = {r['condition_id']: r for r in report['markets']}
            assert rows[CID]['eligible'] is (delay == 2) and rows[CIDS[1]]['eligible'] is True
            assert report['freshness']['decision_as_of'] == ex.now.isoformat()
            if delay == 3:
                assert rows[CID]['skip_reason']
                assert any(p.get('condition_id') == CID and p['at'] == ex.now for _, p in journal.events('diagnostic'))
            canceled = {p['order_id'] for _, p in journal.events('cancel_intent')}
            expected = {oid for oid, o in ex.orders.items() if o.condition_id == CID and o.status == 'CANCELED'}
            assert canceled == expected and len(canceled) == (2 if tick >= 620 else 0)
        assert seen and set(seen) == {'GET'}
        assert not journal.events('merge_intent')
    finally:
        sdk.close()
        observations.close()


def test_multimarket_execution_safety(tmp_path):
    from lp_v2.models import Quote, MergeRequest, ReservationTerms
    from tests.test_live_sdk import request
    from tests.test_scenarios import plan
    from lp_v2.live_runtime import LiveRunner
    runner,ex,sdk,journal,obs=multi_world(tmp_path)
    obs.plans+= (plan('unlisted'),)
    runner.step(AT)
    assert len(ex.posts)==6
    assert {s.token_id for s in ex.posts}=={'101','102','201','202','301','302'}
    assert runner.ledger.snapshot().capital.total==D('30')
    assert all(s.side=='BUY' and s.post_only and s.order_type=='GTD' for s in ex.posts)
    for r in (replace(request(),request_id='foreign',quote=Quote('unlisted','101',D('.4'),D('1'))),
              replace(request(),request_id='mismatch',quote=Quote(CIDS[1],'101',D('.4'),D('1')))):
        with pytest.raises(ValueError):sdk.place_order(r,at=AT)
    with pytest.raises(ValueError):sdk.merge(MergeRequest('bad',OWNER,CIDS[1],'101','102',D('1'),AT),at=AT)
    with pytest.raises(ValueError):sdk.cancel_order('foreign',at=AT)
    # A's expensive inventory cannot lower B's independently proven hedge ceiling.
    ex.fill('order-1','2','.40')
    ex.fill('order-3','10','.30')
    ex.terminal('order-4')
    runner.ledger.reconcile(sdk)
    hedge=replace(request(),request_id='b-hedge',purpose='hedge',quote=Quote(CIDS[1],'202',D('.65'),D('10')),
        reservation_terms=ReservationTerms(PAIRS[1],D('10'),(D('0'),D('0'))))
    assert sdk.place_order(hedge,at=AT).order is not None
    assert journal.events('intent')[-1][1]['hedge_ceiling']==D('.68')
    # Readback must bind condition as well as token and wallet.
    original=ex.orders['order-5']
    ex.orders['order-5']=original.model_copy(update={'condition_id':CIDS[1]})
    with pytest.raises(ValueError):sdk.read_orders()
    ex.orders['order-5']=original
    obs.region={}
    before=len(ex.posts)
    step_at(runner,AT+timedelta(seconds=10))
    assert len(ex.posts)==before
    assert set(c.kwargs['order_id'] for c in ex.sdk.cancel_order.call_args_list) <= set(ex.orders)
    assert all(o.status!='LIVE' for o in ex.orders.values())
    # Separate bounded random histories, including ambiguous placement + restart.
    from random import Random
    for seed in (20261103,20261104,20261105):
        path=tmp_path/str(seed);path.mkdir()
        r,e,s,j,o=multi_world(path);rng=Random(seed);history=[]
        r.step(AT)
        for tick in range(1,16):
            e.now=AT+timedelta(seconds=tick*10)
            active=[x for x in e.orders.values() if x.status=='LIVE']
            if active:
                x=rng.choice(active)
                if rng.choice((True,False)):
                    q=min(D('1'),x.original_size-x.size_matched);e.fill(x.id,q,x.price,at=e.now)
                    history.append(('fill',x.id,str(q)))
                else:e.terminal(x.id);history.append(('terminal',x.id))
            if tick==5:e.sdk.post_order.side_effect=TimeoutError('unknown')
            if tick==6:
                r=LiveRunner(r.config,s,o,clock=lambda:e.now)
                e.sdk.post_order.side_effect=e.post
            r.step(e.now)
            state=r.ledger.snapshot()
            assert D('0')<=state.capital.total<=D('100'),(seed,history)
            cost=D('0')
            for pair in PAIRS:
                a,b=(e.quantities.get(t,D('0')) for t in pair)
                heavy=pair[0] if a>b else pair[1]
                paid=sum((m.price*m.matched_amount for t in e.trades for m in t.maker_orders if m.token_id==heavy),D('0'))
                cost+=paid/max(a,b)*abs(a-b) if max(a,b) else D('0')
            assert cost<=D('50'),(seed,history)
            assert len({x.request_id for x in state.requests})==len(state.requests),(seed,history)
        j.close()
    journal.close()


def factory_inputs(tmp_path, monkeypatch):
    import httpx
    from polymarket import PublicClient, SecureClient
    from polymarket.clients._transport import SyncTransport
    from tests.fakes.live_exchange import Exchange
    from tests.test_readonly_sdk import market_wire
    args,_=live_args(tmp_path)
    relay=tmp_path/'relay.json';relay.write_text(json.dumps(dict(key='synthetic',address=parse_run(args).credentials.signer_address)));relay.chmod(0o600)
    args+=['--relayer-credentials-file',str(relay)]
    config=replace(parse_run(args),started_at=AT)
    Path(config.state_dir).mkdir()
    ex=Exchange(markets=dict(zip(CIDS,PAIRS)))
    ex.sdk.wallet_type='POLY_PROXY';ex.sdk.wallet=config.credentials.wallet_address;ex.sdk.signer=config.credentials.signer_address
    object.__setattr__(ex.sdk,'_ctx',SimpleNamespace(rpc=None,relayer=None,secure_clob=ex.sdk._ctx.secure_clob))
    monkeypatch.setattr(SecureClient,'create',lambda **kw:ex.sdk)
    def sources(pairs=None):
        pairs=pairs or dict(zip(CIDS,PAIRS))
        def handle(req):
            assert req.method=='GET'
            if req.url.path=='/markets/keyset':
                ids=req.url.params.get_list('condition_ids')
                rows=[market_wire(cid,clobTokenIds=list(pair)) for cid,pair in pairs.items() if cid in ids]
                return httpx.Response(200,json={'markets':rows,'next_cursor':None})
            if req.url.path=='/book':
                token=req.url.params['token_id'];cid=next(c for c,p in pairs.items() if token in p)
                return httpx.Response(200,json=dict(market=cid,asset_id=token,timestamp=str(int(AT.timestamp()*1000)),bids=[dict(price='.40',size='100')],asks=[dict(price='.42',size='100')],min_order_size='1',tick_size='.01',neg_risk=False,hash='fixture'))
            return httpx.Response(200,json=dict(in_play=False,blocked=False,country='US'))
        p=PublicClient()
        for name in ('gamma','clob'):
            getattr(p._ctx,name).close()
            p._ctx=replace(p._ctx,**{name:SyncTransport(base_url='https://'+name+'.polymarket.com',client=httpx.Client(base_url='https://'+name+'.polymarket.com',transport=httpx.MockTransport(handle)))})
        return dict(public=p,http=httpx.Client(transport=httpx.MockTransport(handle)),clock=lambda:ex.now)
    return config,ex,sources


@pytest.mark.parametrize('mode',['flat','inventory','live','pending','unknown-order','unknown-merge','wallet','tokens','old-omission','terminal-empty','settled','request-reuse'])
def test_identity_market_transition(tmp_path,monkeypatch,mode):
    from lp_v2.journal import Journal
    from lp_v2.live_runtime import create_live_runner
    from lp_v2.models import Fill,Order,OrderResult,OrderStatus,Outcome,MergeRequest
    from tests.test_live_sdk import request
    config,ex,sources=factory_inputs(tmp_path,monkeypatch)
    owner='lp-v2:'+config.credentials.wallet_address.lower()
    path=Path(config.state_dir)/'journal.sqlite'
    journal=Journal(path)
    legacy=dict(owner=owner,condition_id=CID,token_ids=TOKENS)
    journal.append('identity',legacy);first=journal.db.execute('SELECT payload FROM events WHERE kind="identity"').fetchone()[0]
    journal.close()
    runner=create_live_runner(replace(config,markets=CIDS),**sources())
    assert dict(runner.sdk.markets)==dict(zip(CIDS,PAIRS))
    assert runner.sdk.journal.events('identity')[0][1]==legacy
    assert runner.sdk.journal.db.execute('SELECT payload FROM events WHERE kind="identity" ORDER BY seq LIMIT 1').fetchone()[0]==first
    assert len(runner.sdk.journal.events('identity'))==2
    journal=runner.sdk.journal
    r=replace(request(),owner=owner)
    if mode in ('inventory','live','pending','unknown-order','old-omission'):
        journal.append('intent',dict(request=r,hedge_ceiling=D('.58')))
        if mode!='pending':
            status=OrderStatus.FILLED if mode=='inventory' else OrderStatus.LIVE
            order=Order('owned',r,D('10') if mode=='inventory' else D('0'),D('0') if mode=='inventory' else D('10'),status,AT,mode!='unknown-order')
            journal.append('order_result',OrderResult(r,Outcome.UNKNOWN if mode=='unknown-order' else Outcome.SUCCESS,order,AT))
        if mode=='inventory':journal.append('fill',Fill('fill','owned',r.request_id,owner,CID,'101',D('10'),D('.4'),D('0'),AT))
    if mode=='unknown-merge':
        journal.append('merge_intent',dict(request=MergeRequest('m',owner,CID,'101','102',D('1'),AT),cash=D('100'),positions=()))
    if mode in ('terminal-empty','settled'):
        from lp_v2.models import MergeResult,Position,Quote
        for i,token in enumerate(TOKENS):
            q=D('1') if mode=='settled' else D('0')
            r=replace(request(),owner=owner,request_id=f'terminal-{i}',quote=Quote(CID,token,D('.4') if i==0 else D('.5'),D('10')))
            journal.append('intent',dict(request=r,hedge_ceiling=D('.58')))
            journal.append('order_result',OrderResult(r,Outcome.SUCCESS,Order(f'closed-{i}',r,q,D('0'),OrderStatus.CANCELLED,AT,True),AT))
            if q:journal.append('fill',Fill(f'closed-fill-{i}',f'closed-{i}',r.request_id,owner,CID,token,q,r.quote.price,D('0'),AT))
        if mode=='settled':
            merge=MergeRequest('settled',owner,CID,*TOKENS,D('1'),AT+timedelta(seconds=1))
            consumed=tuple(Position(owner,CID,t,D('1'),D(c),merge.created_at) for t,c in zip(TOKENS,('.4','.5')))
            journal.append('merge_intent',dict(request=merge,positions=consumed,cash=D('99.1')))
            journal.append('merge_result',MergeResult(merge,Outcome.SUCCESS,D('1'),merge.created_at,consumed))
    if mode=='request-reuse':
        journal.append('intent',dict(request=replace(r,owner='foreign'),hedge_ceiling=D('.58')))
        journal.append('intent',dict(request=r,hedge_ceiling=D('.58')))
        journal.append('order_result',OrderResult(r,Outcome.FAILED,None,AT))
    if mode=='wallet':journal.append('identity',dict(legacy,owner='lp-v2:other'))
    runner.sdk.close();runner.observations.close()
    if mode=='old-omission':
        # A corrupt intervening selection must not hide older owned requests.
        journal=Journal(path);journal.append('identity',dict(owner=owner,markets={CIDS[1]:PAIRS[1]}));journal.close()
    selected=replace(config,market=CIDS[1],markets=CIDS[1:])
    if mode=='tokens':selected=replace(config,markets=CIDS)
    kwargs=sources({**dict(zip(CIDS,PAIRS)),CID:('901','902')} if mode=='tokens' else None)
    if mode in ('flat','terminal-empty','settled'):
        removed=create_live_runner(selected,**kwargs)
        assert tuple(removed.sdk.markets)==CIDS[1:]
        assert len(removed.sdk.journal.events('identity'))==3
        removed.sdk.close();removed.observations.close()
    else:
        with pytest.raises(ValueError):create_live_runner(selected,**kwargs)
    assert not ex.posts and ex.sdk.cancel_order.call_count==0


def test_frozen_identity_can_shrink_to_one_market(tmp_path,monkeypatch):
    from lp_v2.live_runtime import LiveRunner,create_live_runner
    config,ex,sources=factory_inputs(tmp_path,monkeypatch)
    runner=create_live_runner(replace(config,markets=CIDS),**sources())
    history=runner.sdk.journal.events()
    runner.sdk.close();runner.observations.close()
    (Path(config.state_dir)/'journal.sqlite').chmod(0o600)
    runner=create_live_runner(replace(config,market=CIDS[1],markets=CIDS[1:2]),**sources())
    try:
        assert isinstance(runner,LiveRunner)
        assert Path(runner.sdk.journal.path)==Path(config.state_dir)/'journal.sqlite'
        assert dict(runner.sdk.markets)=={CIDS[1]:PAIRS[1]}
        assert runner.sdk.journal.events()[:len(history)]==history
        assert len(runner.sdk.journal.events('identity'))==2
        assert not (Path(config.state_dir)/'rotation.sqlite').exists()
        assert not ex.posts and ex.sdk.cancel_order.call_count==0
    finally:
        runner.sdk.close();runner.observations.close()


def seed_financial_day(journal, *, owner=OWNER, day=AT):
    """Hand-worked pairs (.20/.60/.40), fees (.10/.20/.05), gross inventory."""
    from lp_v2.models import Fill,Order,OrderResult,OrderStatus,Outcome,Quote,MergeRequest,MergeResult,Position,ReservationTerms
    from tests.test_live_sdk import request
    for i,(cid,pair,prices,extra,fee) in enumerate(zip(CIDS,PAIRS,(('.40','.50'),('.20','.50'),('.30','.50')),('.50','.20','.60'),('.10','.20','.05'))):
        terms=ReservationTerms(pair,D('10'),(D('0'),D('0')))
        for k,(token,price,qty,costfee) in enumerate(((pair[0],prices[0],'2','0'),(pair[1],prices[1],'2','0'),(pair[0],extra,'1',fee))):
            at=day+timedelta(seconds=3 if k==2 else 1)
            r=replace(request(),owner=owner,request_id=f'{day.date()}-{i}-{k}',quote=Quote(cid,token,D(price),D(qty)),reservation_terms=terms,created_at=day,expires_at=day+timedelta(hours=1))
            journal.append('intent',dict(request=r,hedge_ceiling=D('.98')-D(price)))
            journal.append('order_result',OrderResult(r,Outcome.SUCCESS,Order('order-'+r.request_id,r,D(qty),D('0'),OrderStatus.FILLED,at,True),at))
            journal.append('fill',Fill('fill-'+r.request_id,'order-'+r.request_id,r.request_id,owner,cid,token,D(qty),D(price),D(costfee),at))
        m=MergeRequest(f'{day.date()}-{i}-merge',owner,cid,*pair,D('2'),day+timedelta(seconds=2))
        consumed=tuple(Position(owner,cid,t,D('2'),D(p)*2,m.created_at) for t,p in zip(pair,prices))
        journal.append('merge_intent',dict(request=m,cash=D('90'),positions=consumed))
        journal.append('merge_result',MergeResult(m,Outcome.SUCCESS,D('2'),m.created_at,consumed))


def test_multimarket_summary_and_baselines(tmp_path):
    import httpx
    from tests.test_live_income import earning
    from polymarket.environments import PRODUCTION
    from lp_v2.live_runtime import LiveRunner
    runner,ex,sdk,journal,obs=multi_world(tmp_path)
    seed_financial_day(journal)
    for cid in CIDS:journal.append('day_baseline',dict(day=AT.date().isoformat(),condition_id=cid,at=AT,inventory_pnl=D('0'),source='flat',mark_at=None))
    journal.append('occupation',dict(at=AT,total=D('90'),markets={cid:dict(total=D(v)) for cid,v in zip(CIDS,('20','30','40'))}))
    ex.quantities={'101':D('1'),'201':D('1'),'301':D('1')};ex.cash=D('98.95')
    ex.sdk.list_user_earnings_for_day.side_effect=lambda **kw:(SimpleNamespace(items=tuple(earning(str(n),cid=cid,date=datetime.fromisoformat(kw['date']).replace(tzinfo=UTC)) for cid,n in zip(CIDS,(2,5,8)))),)
    ex.sdk.get_reward_percentages.return_value=dict(zip(CIDS,(10,20,30)))
    def income(req):
        return httpx.Response(200,json=[dict(condition_id=cid,date=req.url.params['date'],maker_address=WALLET,asset_address=PRODUCTION.collateral_token,rebated_fees_usdc=v) for cid,v in zip(CIDS,('1','.5','.25'))])
    sdk.http=httpx.Client(transport=httpx.MockTransport(income))
    now=AT+timedelta(seconds=100)
    obs.plans=tuple(replace(p,books=tuple(replace(b,best_bid=D(mid)-D('.01'),best_ask=D(mid)+D('.01'),observed_at=now) for b in p.books)) for p,mid in zip(obs.plans,('.40','.30','.55')))
    obs.region={}
    result=step_at(runner,now)
    metrics=[r['m4'] for r in result['markets']]
    assert [D(m['net_profit']) for m in metrics]==[D('3.00'),D('6.00'),D('8.55')]
    assert [D(m['net_yield_pct_per_day']) for m in metrics]==[D('15'),D('20'),D('21.375')]
    assert [D(m['paired_profit']) for m in metrics]==[D('.20'),D('.60'),D('.40')]
    assert [D(m['reward_today']) for m in metrics]==[D('2'),D('5'),D('8')]
    audit=journal.events('occupation')[-1][1]
    assert tuple(audit['markets'])==CIDS
    assert sum((m['total'] for m in audit['markets'].values()),D('0'))==audit['total']
    for cid in CIDS:
        m=audit['markets'][cid]
        assert m['inventory'][0]['condition_id']==cid
        assert m['planned_quantity']==D('10')
        assert 'hedges' in m and 'uncertainty' in m
    restart=LiveRunner(runner.config,sdk,obs,clock=lambda:now)
    restarted=[r['m4'] for r in restart.step(now)['markets']]
    assert [{k:D(v) for k,v in m.items()} for m in restarted]==[{k:D(v) for k,v in m.items()} for m in metrics]
    tomorrow=AT+timedelta(days=1)
    obs.plans=tuple(replace(p,books=tuple(replace(b,observed_at=tomorrow) for b in p.books)) for p in obs.plans)
    next_result=step_at(restart,tomorrow)
    assert all(r['m4']['net_profit']=='UNKNOWN' for r in next_result['markets'])
    baselines=[p for _,p in journal.events('day_baseline') if p['day']==tomorrow.date().isoformat()]
    assert {p['condition_id'] for p in baselines}==set(CIDS)
    assert all(p['inventory_pnl'] is None for p in baselines)
    journal.close()
    legacy_path=tmp_path/'legacy';legacy_path.mkdir()
    old_runner,e,s,j,o=multi_world(legacy_path)
    j.append('identity',dict(owner=OWNER,condition_id=CID,token_ids=TOKENS))
    j.append('day_baseline',dict(day='2026-11-02',at=AT,inventory_pnl=D('999')))
    j.append('occupation',dict(at=AT,condition_id=CID,total=D('40')))
    j.append('income',dict(at=now,reward_today=D('111'),rebate_today=D('111'),reward_share=D('1'),scoring={}))
    from lp_v2.live_sdk import LiveSDK
    selected=LiveSDK(e.sdk,j,owner=OWNER,condition_id=CIDS[1],token_ids=PAIRS[1],wallet=WALLET,signer=SIGNER)
    e.sdk.list_user_earnings_for_day.side_effect=ex.sdk.list_user_earnings_for_day.side_effect
    selected.http=httpx.Client(transport=httpx.MockTransport(income))
    o.plans=(obs.plans[1],);o.region={}
    switched=LiveRunner(RunConfig(D('100'),now),selected,o)
    metrics=step_at(switched,now)['markets'][0]['m4']
    assert D(metrics['reward_today'])==D('5') and D(metrics['net_profit'])==D('5.5')
    assert metrics['net_yield_pct_per_day']=='UNKNOWN'
    j.close()
    generation_path=tmp_path/'generation';generation_path.mkdir()
    r,e,s,j,o=multi_world(generation_path)
    from tests.test_live_sdk import request
    from lp_v2.models import Quote,ReservationTerms
    e.reject_tokens.update(TOKENS)
    for i,token in enumerate(TOKENS):
        rejected=replace(request(),request_id=f'old-{i}',quote=Quote(CID,token,D('.4'),D('100')),reservation_terms=ReservationTerms(TOKENS,D('100'),(D('0'),D('0'))))
        assert s.place_order(rejected,at=AT).outcome.value=='failed'
    e.reject_tokens.clear()
    r.step(AT)
    latest=j.events('occupation')[-1][1]
    assert latest['markets'][CID]['planned_quantity']==D('10')
    assert max(x.reservation_terms.generation for x in r.ledger.snapshot().requests if x.quote.condition_id==CID)==1
    j.close()


def report_state(tmp_path, *, omit_minutes=()):
    from lp_v2.journal import Journal
    journal=Journal(tmp_path/'journal.sqlite')
    journal.append('identity',dict(owner='lp-v2:'+WALLET,markets=dict(zip(CIDS,PAIRS)),at=AT-timedelta(days=1)))
    seed_financial_day(journal,owner='lp-v2:'+WALLET)
    for boundary,values in ((AT,('.30','.20','.10')),(AT+timedelta(days=1),('.20','.30','.05'))):
        for cid,value in zip(CIDS,values):
            journal.append('day_baseline',dict(day=boundary.date().isoformat(),condition_id=cid,at=boundary,mark_at=boundary,inventory_pnl=D(value),source='observed_boundary'))
    for minute in range(1441):
        if minute in omit_minutes:continue
        markets={cid:dict(total=D(value),planned_quantity=D('10'),inventory=(),hedges=(),uncertainty=dict(pending=(),orders=(),merges=()),title=f'Market {i}') for i,(cid,value) in enumerate(zip(CIDS,('18','28','38') if minute<720 else ('22','32','42')))}
        journal.append('occupation',dict(at=AT+timedelta(minutes=minute),total=sum((m['total'] for m in markets.values()),D('0')),startup_cap=D('100'),markets=markets,reconciliation='SUCCESS'))
    return journal


def report_credentials(path):
    path.write_text(json.dumps(dict(key='synthetic',secret='c3ludGhldGlj',passphrase='synthetic',signer_address=SIGNER,wallet_address=WALLET,signature_type=1)));path.chmod(0o600)
    return path


def report_transport(monkeypatch, mode='ok'):
    import httpx
    from polymarket.environments import PRODUCTION
    calls=[]
    def handle(transport,req):
        calls.append(req)
        assert req.method=='GET'
        assert req.url.params['date']=='2026-11-02'
        assert req.headers['POLY_API_KEY']=='synthetic'
        if mode=='unavailable':raise TimeoutError('synthetic-secret')
        if req.url.path=='/rewards/user':
            page2=req.url.params.get('next_cursor')=='page2'
            if mode=='incomplete' and page2:return httpx.Response(503,json={})
            rows=[dict(condition_id=cid,date=int(AT.timestamp()*1000),maker_address=WALLET,asset_address=PRODUCTION.collateral_token,asset_rate='1',earnings=value) for cid,value in zip(CIDS,('1','2.5','4'))]
            if mode=='identity':rows[1]['maker_address']=SIGNER
            if mode=='asset':rows[0]['asset_address']='0x'+'9'*40
            if mode=='wrong-day':rows[0]['date']=int((AT+timedelta(days=1)).timestamp()*1000)
            return httpx.Response(200,json=dict(data=rows,next_cursor='LTE=' if page2 else 'page2'))
        if req.url.path=='/rebates/current':
            rows=[dict(condition_id=cid,date='2026-11-02',maker_address=WALLET,asset_address=PRODUCTION.collateral_token,rebated_fees_usdc=v) for cid,v in zip(CIDS,('.5','.25','.125') if mode=='rebate-pages' else ('1','.5','.25'))]
            return httpx.Response(200,json=dict(data=rows,next_cursor='LTE=' if req.url.params.get('next_cursor') else 'rebate-page2') if mode=='rebate-pages' else rows)
        raise AssertionError(req.url.path)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',handle)
    return calls


@pytest.mark.parametrize('mode',['ok','missing-boundary','zero','risk','incomplete','identity','unavailable','gap','missing-risk','unknown-placement','unknown-merge','rebate-pages','asset','wrong-day'])
def test_report_daily_rows_and_safety(tmp_path,monkeypatch,mode):
    from lp_v2.cli import main
    from lp_v2.models import OrderResult,Outcome,Quote,ReservationTerms
    from tests.test_live_sdk import request
    state=tmp_path/'state';state.mkdir();journal=report_state(state,omit_minutes=(701,702) if mode=='gap' else ())
    if mode=='missing-risk':
        journal.append('occupation',dict(at=AT+timedelta(minutes=1),total=D('84'),startup_cap=D('100'),reconciliation='SUCCESS',markets={cid:dict(total=D(v)) for cid,v in zip(CIDS,('18','28','38'))}))
    if mode=='missing-boundary':
        # An explicit later UNKNOWN cannot be replaced by today's latest mark.
        journal.append('day_baseline',dict(day='2026-11-03',condition_id=CID,at=AT+timedelta(days=1),inventory_pnl=None,source='UNKNOWN',mark_at=None))
    if mode=='zero':
        for minute in range(1441):
            journal.append('occupation',dict(at=AT+timedelta(minutes=minute),total=D('0'),startup_cap=D('100'),markets={cid:dict(total=D('0'),planned_quantity=D('0'),inventory=(),hedges=(),uncertainty=dict(pending=(),orders=(),merges=())) for cid in CIDS},reconciliation='SUCCESS'))
    if mode in ('unknown-placement','unknown-merge'):
        from lp_v2.models import MergeRequest
        if mode=='unknown-placement':
            r=replace(request(),owner='lp-v2:'+WALLET,request_id='unsettled-placement')
            journal.append('intent',dict(request=r,hedge_ceiling=D('.58')))
        else:
            journal.append('merge_intent',dict(request=MergeRequest('unsettled-merge','lp-v2:'+WALLET,CID,*TOKENS,D('1'),AT),positions=(),cash=D('90')))
    if mode=='risk':
        journal.append('occupation',dict(at=AT+timedelta(minutes=1),total=D('101'),startup_cap=D('100'),reconciliation='SUCCESS',markets={CID:dict(total=D('101'),planned_quantity=D('1'),inventory=(dict(token_id='101',condition_id=CID,quantity=D('2'),unpaired_quantity=D('2'),unpaired_cost_usdc=D('51')),),hedges=(dict(token_id='102',quantity=D('3'),price=D('.60'),ceiling=D('.58'),request_id='bad'),),uncertainty=dict(pending=('unknown',),orders=(),merges=()))}))
        r=replace(request(),owner='lp-v2:'+WALLET,request_id='unknown',quote=Quote(CID,'101',D('.4'),D('1')),reservation_terms=ReservationTerms(TOKENS,D('1'),(D('0'),D('0'))))
        journal.append('intent',dict(request=r,hedge_ceiling=D('.58')))
        journal.append('order_result',OrderResult(r,Outcome.UNKNOWN,None,AT))
        settled=replace(r,request_id='resolved');journal.append('intent',dict(request=settled,hedge_ceiling=D('.58')))
        journal.append('order_result',OrderResult(settled,Outcome.UNKNOWN,None,AT))
        journal.append('order_result',OrderResult(settled,Outcome.FAILED,None,AT+timedelta(seconds=37)))
    credentials=report_credentials(tmp_path/'l2.json')
    calls=report_transport(monkeypatch,mode)
    out=tmp_path/'reports/daily-2026-11-02'
    assert main(['report','--state-dir',str(state),'--day','2026-11-02','--out',str(out),'--credentials-file',str(credentials)])==0
    result=json.loads(out.with_suffix('.json').read_text())
    rows=result['rows'];assert [r['condition_id'] for r in rows]==[*CIDS,'TOTAL']
    assert out.with_suffix('.csv').is_file()
    assert {r.url.path for r in calls}=={'/rewards/user','/rebates/current'}
    assert sum(r.url.path=='/rewards/user' for r in calls)==(1 if mode in ('identity','unavailable','asset','wrong-day') else 2)
    if mode in ('ok','rebate-pages'):
        expected=[('2','1','.20','.10','-.10','3.00','20','15'),('5','.5','.60','.20','.10','6.00','30','20'),('8','.25','.40','.05','-.05','8.55','40','21.375'),('15','1.75','1.20','.35','-.05','17.55','90','19.5')]
        columns=('reward','rebate','paired_profit','fees','inventory_delta','net_profit','average_occupation','yield_pct_per_day')
        for row,values in zip(rows,expected):assert [D(row[k]) for k in columns]==list(map(D,values))
        assert result['safety']['capital']['status']=='PASS'
        assert result['safety']['unpaired']['status']=='PASS'
    if mode in ('unknown-placement','unknown-merge'):
        assert rows[0]['net_profit']=='UNKNOWN' and rows[0]['notes']
    if mode=='gap':
        assert all(r['average_occupation']=='UNKNOWN' for r in rows)
        assert result['safety']['capital']['status']=='UNKNOWN'
    if mode=='missing-risk':
        assert all(result['safety'][k]['status']=='UNKNOWN' for k in ('unpaired','hedge_ceiling','duplicate_hedge'))
    if mode=='missing-boundary':
        assert rows[0]['inventory_delta']==rows[0]['net_profit']=='UNKNOWN'
        assert rows[0]['notes']
    if mode=='zero':assert all(r['yield_pct_per_day']=='UNKNOWN' and D(r['average_occupation'])==0 for r in rows)
    if mode=='risk':
        assert [result['safety'][k]['status'] for k in ('capital','unpaired','hedge_ceiling','duplicate_hedge')]==['FAIL']*4
        episodes=result['safety']['unknown_duration']['episodes']
        assert next(e for e in episodes if e['identity']=='resolved')['seconds']=='37.0'
        assert next(e for e in episodes if e['identity']=='unknown')['resolved_at'] is None
    assert result['safety']['unknown_duration']['status']=='UNKNOWN'
    assert 'threshold' in result['safety']['unknown_duration']['reason']
    if mode in ('incomplete','identity','unavailable','asset','wrong-day'):
        assert all(r['reward']=='UNKNOWN' and r['net_profit']=='UNKNOWN' and r['notes'] for r in rows)
    journal.close()


@pytest.mark.parametrize('mode',['normal','non-get','no-credentials'])
def test_report_readonly_boundary(tmp_path,monkeypatch,capsys,mode):
    import builtins
    import sqlite3
    import httpx
    from eth_account import Account
    from polymarket import SecureClient
    from lp_v2.cli import main
    state=tmp_path/'state';state.mkdir();journal=report_state(state)
    control=state/'process.json';control.write_text('{"pid":42}')
    # This committed title exists only in the live WAL, not an immutable main-file view.
    journal.append('occupation',dict(at=AT+timedelta(hours=1),total=D('84'),startup_cap=D('100'),reconciliation='SUCCESS',markets={cid:dict(total=D(v),title='WAL committed',inventory=(),hedges=(),planned_quantity=D('0')) for cid,v in zip(CIDS,('18','28','38'))}))
    journal.append('occupation',dict(at=AT+timedelta(hours=23,minutes=59),total=D('96'),startup_cap=D('100'),reconciliation='SUCCESS',markets={cid:dict(total=D(v),title='WAL committed',inventory=(),hedges=(),planned_quantity=D('0')) for cid,v in zip(CIDS,('22','32','42'))}))
    credentials=report_credentials(tmp_path/'l2.json')
    def forbidden(*args,**kw):raise AssertionError('signing or private-key access forbidden')
    monkeypatch.setattr(Account,'from_key',forbidden);monkeypatch.setattr(SecureClient,'create',forbidden)
    opened=[];real_open=builtins.open
    def guarded_open(file,*args,**kw):
        assert 'signing' not in str(file) and 'relayer' not in str(file)
        opened.append(str(file));return real_open(file,*args,**kw)
    monkeypatch.setattr(builtins,'open',guarded_open)
    statements=[];real_connect=sqlite3.connect
    def connect(database,*args,**kw):
        assert kw.get('uri') is True and 'mode=ro' in str(database) and 'immutable' not in str(database)
        db=real_connect(database,*args,**kw)
        def authorize(action,a,b,db_name,trigger):
            assert action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_TRANSACTION), (action,a,b)
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize);db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(sqlite3,'connect',connect)
    requests=report_transport(monkeypatch)
    if mode=='non-get':
        original_send=httpx.Client.send
        def malicious(client,request,*args,**kw):
            request.method='POST'
            return original_send(client,request,*args,**kw)
        monkeypatch.setattr(httpx.Client,'send',malicious)
    snapshot={p.name:p.read_bytes() for p in state.iterdir() if p.is_file() and not p.name.endswith('-shm')}
    out=tmp_path/'outside/daily'
    argv=['report','--state-dir',str(state),'--day','2026-11-02','--out',str(out)]
    if mode!='no-credentials':argv+=['--credentials-file',str(credentials)]
    assert main(argv)==0
    result=json.loads(out.with_suffix('.json').read_text())
    assert result['rows'][0]['title']=='WAL committed'
    # SQLite read locks may update SHM read marks; durable DB/WAL and controls must be byte-identical.
    assert {p.name:p.read_bytes() for p in state.iterdir() if p.is_file() and not p.name.endswith('-shm')}==snapshot
    assert statements and all('CREATE' not in s and 'PRAGMA' not in s and 'INSERT' not in s for s in statements)
    if mode in ('non-get','no-credentials'):
        assert requests==[]
        assert result['rows'][0]['reward']=='UNKNOWN' and result['rows'][0]['notes']
    else:assert requests and all(r.method=='GET' for r in requests)
    alias=tmp_path/'state-alias';alias.symlink_to(state,target_is_directory=True)
    hard=tmp_path/'alias.json';os.link(state/'journal.sqlite',hard)
    control_alias=tmp_path/'control.json';os.link(control,control_alias)
    for invalid in (str(state/'journal.sqlite'),str(state/'process.json'),str(alias/'report'),str(hard),str(control_alias)):
        bad=argv.copy();bad[bad.index('--out')+1]=invalid
        with pytest.raises(SystemExit):main(bad)
    for invalid in ('2026-02-30','20261102','2026-1-02','not-a-date'):
        bad=argv.copy();bad[bad.index('--day')+1]=invalid
        with pytest.raises(SystemExit):main(bad)
    with pytest.raises(SystemExit):main(argv+['--private-key-file','forbidden-signing'])
    captured=capsys.readouterr()
    assert WALLET not in captured.out and SIGNER not in captured.out and 'synthetic' not in captured.out
    assert not any('order-' in word for word in captured.out.split())
    assert out.with_suffix('.csv').is_file()
    journal.close()


def test_seven_day_round_latency(tmp_path,capsys):
    import signal
    import time
    from lp_v2.journal import Journal, encode, decode
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.models import Quote, ReservationTerms
    from tests.test_live_sdk import request
    runner,ex,sdk,journal,obs=multi_world(tmp_path)
    start=AT-timedelta(days=6)
    breakdown={cid:dict(inventory_cost=D('0'),open_buys=D('9.5'),pending_requests=D('0'),hedge_reserve=D('.5'),fee_reserve=D('0'),total=D('10'),planned_quantity=D('10'),hedges=(),uncertainty=dict(pending=(),orders=(),merges=()),title='Synthetic political market',inventory=()) for cid in CIDS}
    observations=0
    generated_at=time.perf_counter()
    # Generate each hourly GTD generation through the real SDK/SQLite append path,
    # then concatenate its facts. Bounded staging keeps fixture construction cheap.
    for hour in range(168):
        staging=Journal(':memory:')
        source=LiveSDK(ex.sdk,staging,owner=OWNER,condition_id=CID,token_ids=TOKENS,markets=dict(zip(CIDS,PAIRS)),wallet=WALLET,signer=SIGNER,clock=lambda:ex.now)
        created=start+timedelta(hours=hour)
        ex.now=created
        active=[]
        for cid,pair in zip(CIDS,PAIRS):
            for token,price in zip(pair,(D('.40'),D('.55'))):
                r=replace(request(),request_id=f'hour-{hour}-{token}',quote=Quote(cid,token,price,D('10')),created_at=created,expires_at=created+timedelta(hours=1),reservation_terms=ReservationTerms(pair,D('10'),(D('0'),D('0')),hour))
                active.append(source.place_order(r,at=created).order.order_id)
        rows=[]
        for tick in range(360):
            ex.now=created+timedelta(seconds=10*tick)
            if hour==167 and tick==180:
                ex.fill(active[0],'1','.40',at=ex.now)
            before=ex.sdk.get_order.call_count
            facts=source.read_orders()
            assert ex.sdk.get_order.call_count-before==6
            assert len(facts)==6 and all(o.authoritative and o.observed_at==ex.now for o in facts)
            observations+=6
            rows.append(('occupation',encode(dict(at=ex.now,condition_id=CID,inventory_cost=D('0'),open_buys=D('28.5'),pending_requests=D('0'),hedge_reserve=D('1.5'),fee_reserve=D('0'),total=D('30'),startup_cap=D('100'),markets=breakdown,inventory=(),uncertainty=dict(pending=(),orders=(),merges=()),reconciliation='SUCCESS',accounting_as_of=ex.now))))
            if tick%6==0:
                rows.extend(('income',encode(dict(at=ex.now,condition_id=cid,reward_today=D('0'),rebate_today=D('0'),reward_share=D('0'),scoring={oid:True for oid in active}))) for cid in CIDS)
        source.read_trades()
        if hour<167:
            ex.now=created+timedelta(seconds=3599)
            for oid in active:source.cancel_order(oid,at=ex.now)
            source.read_orders()
        with journal.db:
            journal.db.executemany('INSERT INTO events(kind,payload) VALUES (?,?)',staging.db.execute('SELECT kind,payload FROM events ORDER BY seq'))
            journal.db.executemany('INSERT INTO events(kind,payload) VALUES (?,?)',rows)
        staging.close()
        ex.sdk.reset_mock()
    generated=time.perf_counter()-generated_at
    counts=dict(journal.db.execute('SELECT kind,count(*) FROM events GROUP BY kind'))
    assert counts['occupation']==60480 and counts['income']==30240 and observations==362880
    assert counts['intent']==counts['signed']==1008
    assert counts['cancel_intent']==1002 and counts['fill']==1
    assert counts['order_result']==4021  # 1008 accepted + 1008 authority + 1002 cancel + 1002 terminal + one fill
    size=sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file())
    now=AT+timedelta(days=1,seconds=-10)
    assert ex.now==now
    obs.plans=tuple(replace(p,books=tuple(replace(b,observed_at=now) for b in p.books)) for p in obs.plans)
    traced=[];journal.db.set_trace_callback(traced.append)
    old=signal.getsignal(signal.SIGALRM)
    def watchdog(*args):raise AssertionError('independent real-time watchdog expired')
    signal.signal(signal.SIGALRM,watchdog);signal.setitimer(signal.ITIMER_REAL,45)
    try:
        tick=time.perf_counter();result=runner.step(now);elapsed=time.perf_counter()-tick
    finally:signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,old)
    with capsys.disabled():
        print(f'M5 latency: {elapsed:.6f}s; generation {generated:.3f}s; 60480 rounds; {observations} live readbacks; counts {counts}; dataset {size} bytes',flush=True)
    assert elapsed<=5
    production_traced=traced.copy()
    audit=decode(journal.db.execute("SELECT payload FROM events WHERE kind='occupation' ORDER BY seq DESC LIMIT 1").fetchone()[0])
    assert audit['reconciliation']=='SUCCESS' and audit['total']<=100 and set(audit['markets'])==set(CIDS)
    assert {r['condition_id'] for r in result['markets']}==set(CIDS)
    occupation_reads=[sql for sql in production_traced if sql.startswith('SELECT kind,payload') and "'occupation'" in sql]
    assert occupation_reads and all('seq>=' in sql for sql in occupation_reads),occupation_reads
    # Restart and UTC rollover establish the correct bound, without a stale fact cache.
    for at in (now,AT+timedelta(days=1)):
        traced.clear();ex.now=at
        r=LiveRunner(runner.config,sdk,obs,clock=lambda:ex.now)
        obs.plans=tuple(replace(p,books=tuple(replace(b,observed_at=at) for b in p.books)) for p in obs.plans)
        r.step(at)
        reads=[sql for sql in traced if sql.startswith('SELECT kind,payload') and "'occupation'" in sql]
        assert reads and all('seq>=' in sql for sql in reads)
        if at.date()!=now.date():
            import re
            assert all(int(re.search(r'seq>=(\d+)',sql).group(1))>60480 for sql in reads),reads
    signal.signal(signal.SIGALRM,watchdog);signal.setitimer(signal.ITIMER_REAL,90)
    try:
        tick=time.perf_counter();code=r.drain(timeout_s=90);drained=time.perf_counter()-tick
    finally:signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,old)
    with capsys.disabled():
        print(f'M5 drain: {drained:.6f}s; exit {code}',flush=True)
    assert drained<=90 and code==0
    assert not sdk.known_open_order_ids() and not any(o.status=='LIVE' for o in ex.orders.values())
    stopped=journal.events('stop_result')[-1][1]
    assert stopped['exit_code']==0 and stopped['reconciliation']=='SUCCESS' and not stopped['pending']
    journal.close()

@pytest.mark.parametrize('status,authoritative,resolved',[
    ('live',True,False),('cancelled',False,False),('cancelled',True,True),('filled',True,True),
],ids=['live-unresolved','non-authoritative-terminal','cancelled-resolved','filled-resolved'])
def test_report_cancel_resolution(tmp_path,status,authoritative,resolved):
    from lp_v2.cli import main
    from lp_v2.journal import Journal
    from lp_v2.models import Fill, Order, OrderResult, OrderStatus, Outcome
    from tests.test_live_sdk import request
    state=tmp_path/'state';state.mkdir();journal=Journal(state/'journal.sqlite')
    owner='lp-v2:'+WALLET
    r=replace(request(),owner=owner)
    journal.append('identity',dict(owner=owner,condition_id=CID,token_ids=TOKENS))
    journal.append('intent',dict(request=r,hedge_ceiling=D('.58')))
    accepted=Order('own-order',r,D('0'),D('10'),OrderStatus.LIVE,AT,False)
    journal.append('order_result',OrderResult(r,Outcome.SUCCESS,accepted,AT))
    at=AT+timedelta(seconds=5)
    journal.append('order_result',OrderResult(r,Outcome.SUCCESS,replace(accepted,observed_at=at,authoritative=True),at))
    for seconds in (10,15):journal.append('cancel_intent',dict(order_id='own-order',at=AT+timedelta(seconds=seconds)))
    observed=AT+timedelta(seconds=20)
    quantity=D('10') if status=='filled' else D('0')
    final=Order('own-order',r,quantity,D('10') if status=='live' else D('0'),OrderStatus(status),observed,authoritative,AT+timedelta(seconds=10))
    journal.append('order_result',OrderResult(r,Outcome.SUCCESS if authoritative else Outcome.UNKNOWN,final,observed))
    if quantity:
        journal.append('fill',Fill('own-fill','own-order',r.request_id,owner,CID,'101',quantity,D('.4'),D('0'),AT+timedelta(seconds=19)))
    out=tmp_path/'reports/cancel'
    assert main(['report','--state-dir',str(state),'--day','2026-11-02','--out',str(out)])==0
    result=json.loads(out.with_suffix('.json').read_text())
    episodes=[e for e in result['safety']['unknown_duration']['episodes'] if e['kind']=='cancel']
    assert len(episodes)==1
    episode=episodes[0]
    assert episode['started_at']==(AT+timedelta(seconds=10)).isoformat()
    if resolved:
        assert episode['resolved_at']==observed.isoformat() and D(episode['seconds'])==D('10')
    else:
        assert episode['resolved_at'] is None
        expected=D(str(max(0.,(datetime.fromisoformat(result['as_of'])-(AT+timedelta(seconds=10))).total_seconds())))
        assert D(episode['seconds'])==expected
    placement=[e for e in result['safety']['unknown_duration']['episodes'] if e['kind']=='placement' and e['resolved_at'] is not None]
    assert len(placement)==1 and D(placement[0]['seconds'])==D('5')
    journal.close()


def test_report_fresh_readback_duration(tmp_path):
    from lp_v2.cli import main
    from lp_v2.journal import Journal
    from lp_v2.live_sdk import LiveSDK
    from tests.fakes.live_exchange import Exchange
    from tests.test_live_sdk import request
    state=tmp_path/'state';state.mkdir();journal=Journal(state/'journal.sqlite')
    owner='lp-v2:'+WALLET
    journal.append('identity',dict(owner=owner,condition_id=CID,token_ids=TOKENS))
    ex=Exchange()
    sdk=LiveSDK(ex.sdk,journal,owner=owner,condition_id=CID,token_ids=TOKENS,wallet=WALLET,signer=SIGNER,clock=lambda:ex.now)
    r=replace(request(),owner=owner)
    accepted=sdk.place_order(r,at=AT)
    assert not accepted.order.authoritative and accepted.observed_at==AT
    ex.now=AT+timedelta(seconds=60)
    readback=sdk.read_orders()[0]
    out=tmp_path/'reports/placement'
    assert main(['report','--state-dir',str(state),'--day','2026-11-02','--out',str(out)])==0
    result=json.loads(out.with_suffix('.json').read_text())
    episode=next(e for e in result['safety']['unknown_duration']['episodes'] if e['kind']=='placement' and e['started_at']==AT.isoformat())
    assert D(episode['seconds'])==D('60') and episode['resolved_at']==ex.now.isoformat()
    assert readback.authoritative and readback.observed_at==ex.now
    assert journal.events('order_result')[-1][1].observed_at==ex.now
    ex.now=AT+timedelta(seconds=70)
    sdk.cancel_order(accepted.order.order_id,at=ex.now)
    ex.now=AT+timedelta(seconds=90)
    terminal=sdk.read_orders()[0]
    out=tmp_path/'reports/cancel'
    assert main(['report','--state-dir',str(state),'--day','2026-11-02','--out',str(out)])==0
    result=json.loads(out.with_suffix('.json').read_text())
    cancellation=next(e for e in result['safety']['unknown_duration']['episodes'] if e['kind']=='cancel')
    assert D(cancellation['seconds'])==D('20') and cancellation['resolved_at']==ex.now.isoformat()
    assert terminal.authoritative and terminal.status.value=='cancelled' and terminal.observed_at==ex.now
    assert terminal.request.created_at==AT and terminal.request.expires_at==AT+timedelta(hours=1)
    assert len(ex.posts)==1 and ex.sdk.cancel_order.call_count==1
    ex.now+=timedelta(seconds=10)
    assert sdk.read_orders()==(terminal,) and len(ex.posts)==1
    assert all(p.request.created_at==AT and p.request.expires_at==AT+timedelta(hours=1) for _,p in journal.events('order_result'))
    journal.close()
