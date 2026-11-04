from datetime import UTC, datetime
from decimal import Decimal as D
import httpx
import pytest

AT = datetime(2026, 11, 2, tzinfo=UTC)


def test_readonly_adapter_has_no_mutation_surface():
    from lp_v2.readonly_sdk import ReadOnlySDK
    from polymarket import PublicClient
    public = PublicClient()
    sdk = ReadOnlySDK(public=public, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    for name in ('place_order', 'cancel_order', 'cancel_all', 'merge', 'create_limit_order', 'post_order', 'create_api_key', 'request', 'get', 'public', 'transport'):
        assert not hasattr(sdk, name)
    assert sdk.read_wallet(AT)['available_usdc'] == 'UNKNOWN'
    assert sdk.safety(AT) == dict(orders_sent=0, cancels_sent=0, since=AT)
    sdk.close()


CID = '0x' + '1' * 64
BAD = '0x' + '2' * 64


def market_wire(cid=CID, **overrides):
    return dict(dict(id='1', conditionId=cid, question='央行?', outcomes=['Yes','No'], clobTokenIds=['101','102'],
                active=True, closed=False, acceptingOrders=True, enableOrderBook=True,
                orderMinSize='1', orderPriceMinTickSize='0.01', feesEnabled=False,
                rewardsMinSize='10', rewardsMaxSpread=5,
                clobRewards=[dict(id='1', conditionId=cid, assetAddress='0x2791bca1f2de4661ed88a30c99a7a9449aa84174', rewardsAmount='100', rewardsDailyRate='100', startDate='2026-01-01')],
                bestBid='0.46', bestAsk='0.48', liquidityNum='2000', volume24hr='10000',
                tags=[dict(id='2', label='Politics', slug='politics')]), **overrides)


def external_sdk(rows=None, book_mismatch=False, book_prices=None):
    from polymarket import PublicClient
    from polymarket.clients._transport import SyncTransport
    seen=[]
    def handle(req):
        seen.append((req.method, str(req.url)))
        assert req.method == 'GET'
        if req.url.path == '/markets/keyset':
            # SDK keyset paginator: second page empty when cursor is returned.
            if req.url.params.get('after_cursor'):
                return httpx.Response(200, json={'markets': [], 'next_cursor': None})
            return httpx.Response(200, json={'markets': rows if rows is not None else [market_wire(), market_wire(BAD, bestBid='0.92', bestAsk='0.94')], 'next_cursor':'page2'})
        if req.url.path == '/book':
            token=req.url.params['token_id']; bid,ask=(book_prices[token] if book_prices is not None else ('0.46','0.48') if token=='101' else ('0.51','0.53'))
            return httpx.Response(200,json=dict(market=BAD if book_mismatch else CID,asset_id=token,timestamp='1793577600000',
                bids=[dict(price=bid,size='100')],asks=[dict(price=ask,size='100')],min_order_size='1',tick_size='0.01',neg_risk=False,hash='fixture'))
        if req.url.path.startswith('/markets/'):
            return httpx.Response(200,json={'in_play':False})
        raise AssertionError(str(req.url))
    p=PublicClient()
    for name in ('gamma','clob'):
        getattr(p._ctx,name).close()
        from dataclasses import replace
        p._ctx=replace(p._ctx, **{name: SyncTransport(base_url='https://'+name+'.polymarket.com', client=httpx.Client(base_url='https://'+name+'.polymarket.com', transport=httpx.MockTransport(handle)))})
    return p,seen


def test_public_market_and_book_reads():
    from lp_v2.readonly_sdk import ReadOnlySDK
    from lp_v2.quoting import quote_pair
    p,seen=external_sdk()
    sdk=ReadOnlySDK(public=p, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={'in_play':False}))))
    rows=sdk.read_markets(AT)
    assert len(rows)==2
    good,bad=rows
    assert good['title']=='央行?' and good['skip_reason'] is None
    plan=good['plan']; m=plan.market
    assert tuple(t.token_id for t in m.tokens)==('101','102')
    assert (m.tick_size,m.min_order_size,m.rewards_min_size)==(D('0.01'),D('1'),D('10'))
    assert m.rewards_daily_rate==100 and m.rewards_max_spread==5
    assert plan.fee_per_share==(D('0'),D('0')) and not m.fees_enabled
    pair=quote_pair(m,*plan.books,planned_quantity=D('10'),fee_per_pair=D('0'),safety_cushion=D('0.02'))
    assert [q.price for q in pair]==[D('0.46'),D('0.51')]
    assert sum(q.price for q in pair)<=D('0.98')
    assert bad['plan'] is None and bad['skip_reason']
    assert all(method=='GET' for method,url in seen)
    sdk.close()
    for wire,mismatch,score in [(market_wire(feesEnabled=True),False,'UNKNOWN'),(market_wire(rewardsMaxSpread=None),False,'UNKNOWN'),(market_wire(),True,'3.4643')]:
        p,_=external_sdk([wire],mismatch); sdk=ReadOnlySDK(public=p, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={'in_play':False}))))
        row=sdk.read_markets(AT)[0]
        assert row['plan'] is None and row['eligible'] is False and row['score']==score and row['skip_reason']
        sdk.close()


def test_wallet_read_uses_existing_l2_credentials_only(caplog):
    import base64, hashlib, hmac
    from lp_v2.readonly_sdk import ReadOnlySDK, L2Credentials
    creds=L2Credentials('synthetic-key',base64.urlsafe_b64encode(b'synthetic-secret').decode(),'synthetic-pass', '0x'+'a'*40,'0x'+'b'*40,2)
    seen=[]
    def handle(r):
        seen.append(r)
        assert r.method=='GET' and r.url.host=='clob.polymarket.com'
        assert r.url.path in ('/balance-allowance','/data/orders')
        message=f'{int(AT.timestamp())}GET{r.url.path}'.encode()
        assert r.headers['POLY_SIGNATURE']==base64.urlsafe_b64encode(hmac.new(b'synthetic-secret',message,hashlib.sha256).digest()).decode()
        assert r.headers['POLY_ADDRESS']=='0x'+'a'*40
        if r.url.path=='/balance-allowance':
            assert dict(r.url.params)==dict(asset_type='COLLATERAL',signature_type='2')
            return httpx.Response(200,json=dict(balance='182400000',allowances={}))
        return httpx.Response(200,json=dict(data=[dict(id='legacy')],next_cursor='LTE='))
    sdk=ReadOnlySDK(http=httpx.Client(transport=httpx.MockTransport(handle)),credentials=creds)
    assert sdk.read_wallet(AT)==dict(available_usdc='182.4',as_of=AT)
    assert sdk.read_orders(AT)==dict(legacy_order_count=1,as_of=AT)
    assert len(seen)==2 and sdk.safety(AT)['orders_sent']==0
    assert 'synthetic' not in repr(creds)+repr(sdk)+caplog.text
    sdk.close()
    for payload in ({},dict(balance='NaN',allowances={}),dict(balance='-1',allowances={}),dict(balance=True,allowances={})):
        sdk=ReadOnlySDK(http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload))),credentials=creds)
        assert sdk.read_wallet(AT)==dict(available_usdc='UNKNOWN',as_of=None)
        sdk.close()
    def fail(r): raise RuntimeError('synthetic-secret '+creds.signer_address)
    sdk=ReadOnlySDK(http=httpx.Client(transport=httpx.MockTransport(fail)),credentials=creds)
    assert sdk.read_wallet(AT)==dict(available_usdc='UNKNOWN',as_of=None)
    assert 'synthetic' not in caplog.text
    sdk.close()


@pytest.mark.parametrize('dropped',['liquidity','midpoint'])
def test_fixed_market_recovery_keeps_opening_ineligible_plan(tmp_path,dropped):
    from datetime import timedelta
    from lp_v2.readonly_sdk import ReadOnlySDK
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.journal import Journal
    from lp_v2.cli import RunConfig
    from lp_v2.ledger import Ledger
    from lp_v2.engine import Engine
    from lp_v2.models import Decision
    from tests.fakes.live_exchange import Exchange
    from tests.test_live_sdk import OWNER,WALLET,SIGNER,TOKENS
    import pytest
    wire=market_wire()
    prices={'101':('0.46','0.48'),'102':('0.51','0.53')}
    public,_=external_sdk(rows=[wire],book_prices=prices)
    in_play=[False]
    http=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=dict(
        in_play=in_play[0],blocked=False,country='US'))))
    observations=ReadOnlySDK(public=public,http=http)
    ex=Exchange(condition_id=CID)
    journal=Journal(tmp_path/'journal.sqlite')
    live=LiveSDK(ex.sdk,journal,owner=OWNER,condition_id=CID,token_ids=TOKENS,wallet=WALLET,signer=SIGNER)
    runner=LiveRunner(RunConfig(D('100'),AT),live,observations)
    ex.reject_tokens.add('102')
    try:
        runner.step(AT)
        assert len(ex.posts)==2
        ex.fill('order-1','10','.46',at=AT+timedelta(seconds=1))
        if dropped=='liquidity':
            wire['liquidityNum']='500'
        else:
            wire.update(bestBid='.92',bestAsk='.94')
            prices.update({'101':('.92','.94'),'102':('.05','.07')})
        row=observations.read_fixed_market(AT+timedelta(seconds=10),CID)[0]
        assert row['eligible'] is False and row['plan'] is not None
        assert observations.read_markets(AT+timedelta(seconds=10))[0]['plan'] is None
        assert Engine(Ledger(OWNER,D('100'))).decide((row['plan'],),now=AT+timedelta(seconds=10))==Decision()
        ex.reject_tokens.clear()
        runner.step(AT+timedelta(seconds=10))
        hedges=[r for r in runner.ledger.snapshot().requests if r.purpose=='hedge']
        assert len(hedges)==1 and hedges[0].quote.quantity==D('10')
        assert hedges[0].quote.price==D('.51' if dropped=='liquidity' else '.05')
        assert hedges[0].quote.price<=D('.52')
        for changes in ({'feesEnabled':True},{'acceptingOrders':False},{'orderPriceMinTickSize':'.02'}):
            original={k:wire[k] for k in changes}
            wire.update(changes)
            with pytest.raises(ValueError):
                observations.read_fixed_market(AT+timedelta(seconds=10),CID)
            wire.update(original)
        in_play[0]=True
        with pytest.raises(ValueError):
            observations.read_fixed_market(AT+timedelta(seconds=10),CID)
        in_play[0]=False
        with pytest.raises(ValueError):
            observations.read_fixed_market(AT+timedelta(seconds=70),CID)
    finally:
        live.close();observations.close()


def taker_fee_wire(**overrides):
    return market_wire(feesEnabled=True, feeSchedule=dict(rate='.05', exponent=1, takerOnly=True, rebateRate='.25'), **overrides)


def test_fixed_market_accepts_verified_taker_only_fees():
    from lp_v2.readonly_sdk import ReadOnlySDK
    p, seen = external_sdk([taker_fee_wire()])
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={'in_play':False}))) as http:
        sdk = ReadOnlySDK(public=p,http=http)
        plan = sdk.read_fixed_market(AT,CID)[0]['plan']
        assert plan.market.condition_id == CID
        assert tuple(t.token_id for t in plan.market.tokens) == ('101','102')
        assert plan.fee_per_share == (D('0'),D('0'))
        assert plan.market.fees_enabled is True
        assert plan.market.taker_fee_bps == 500 and plan.market.rebate_rate == .25
        assert all(method == 'GET' for method,_ in seen)
        sdk.close()


FEE_FAILURES = ['missing-schedule','missing-role','non-taker-only','unsupported-exponent',
                'negative-rate','nan-rate','invalid-rebate','unknown-fee-flag','contradictory-fee-flag']


def unproven_fee_wire(case):
    wire = taker_fee_wire()
    if case == 'missing-schedule': wire.pop('feeSchedule')
    elif case == 'missing-role': wire['feeSchedule'].pop('takerOnly')
    elif case == 'non-taker-only': wire['feeSchedule']['takerOnly'] = False
    elif case == 'unsupported-exponent': wire['feeSchedule']['exponent'] = 2
    elif case == 'negative-rate': wire['feeSchedule']['rate'] = '-.01'
    elif case == 'nan-rate': wire['feeSchedule']['rate'] = 'NaN'
    elif case == 'invalid-rebate': wire['feeSchedule']['rebateRate'] = '1.1'
    elif case == 'unknown-fee-flag': wire['feesEnabled'] = None
    elif case == 'contradictory-fee-flag': wire['feesEnabled'] = False
    return wire


@pytest.mark.parametrize('case', FEE_FAILURES)
def test_fixed_market_rejects_unproven_fee_policy(case):
    from lp_v2.readonly_sdk import ReadOnlySDK
    p,_ = external_sdk([unproven_fee_wire(case)])
    sdk = ReadOnlySDK(public=p,http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={'in_play':False}))))
    try:
        with pytest.raises(ValueError):
            sdk.read_fixed_market(AT,CID)
    finally:
        sdk.close()


def test_conditional_inventory_is_independent_of_opening_eligibility():
    from eth_account import Account
    from polymarket.environments import PRODUCTION
    from polymarket._internal.wallet import derive_proxy_wallet_address
    from lp_v2.readonly_sdk import ReadOnlySDK,L2Credentials
    signer=Account.from_key(bytes([7])*32).address
    creds=L2Credentials('synthetic','c2VjcmV0','synthetic',signer,derive_proxy_wallet_address(signer,PRODUCTION.wallet_derivation),1)
    scenario={};seen=[]
    def http(request):
        seen.append(request)
        assert request.method=='GET'
        if request.url.path.startswith('/markets/'):return httpx.Response(200,json={'in_play':True}) # opening remains independently blocked
        token=request.url.params['token_id']
        assert token in ('101','102') and request.url.params['asset_type']=='CONDITIONAL'
        assert request.headers['POLY_ADDRESS']==signer
        return httpx.Response(scenario.get('status',200),json={'balance':scenario.get('balance','0' if token=='101' else '1234567'),'allowances':{}})
    for wire in (market_wire(liquidityNum='825.3674'),market_wire()):
        public,_=external_sdk([wire]);sdk=ReadOnlySDK(public=public,http=httpx.Client(transport=httpx.MockTransport(http)),credentials=creds)
        try:
            assert sdk.public_preflight(AT,CID)['in_play']['status']=='BLOCKED'
            data=sdk.read_conditional_inventory(AT,CID)
            assert data==dict(condition_id=CID,token_ids=('101','102'),quantities={'101':D('0'),'102':D('1.234567')},as_of=AT.isoformat())
            for bad in (True,'NaN','-1','0.5',None):
                scenario['balance']=bad
                with pytest.raises(ValueError):sdk.read_conditional_inventory(AT,CID)
            scenario.clear();scenario['status']=503
            with pytest.raises(Exception):sdk.read_conditional_inventory(AT,CID)
            scenario.clear()
        finally:sdk.close()
    for wire in (market_wire(BAD),market_wire(clobTokenIds=['101','101']),market_wire(outcomes=['No','Yes']),market_wire(clobTokenIds=['101'])):
        public,_=external_sdk([wire]);sdk=ReadOnlySDK(public=public,http=httpx.Client(transport=httpx.MockTransport(http)),credentials=creds)
        before=len(seen)
        try:
            with pytest.raises(ValueError):sdk.read_conditional_inventory(AT,CID)
            assert len(seen)==before
        finally:sdk.close()


@pytest.mark.parametrize('case',['missing','false','true','null','zero','one','string','array','nonobject','json_null','invalid_json','http_failure','timeout'])
def test_fixed_market_accepts_false_or_absent_in_play(monkeypatch,case):
    import json
    from lp_v2.readonly_sdk import ReadOnlySDK
    from polymarket import SecureClient
    seen=[]
    def http(request):
        seen.append(request)
        assert request.method=='GET' and request.url.path=='/markets/'+CID
        replies={'missing':{},'false':{'in_play':False},'true':{'in_play':True},'null':{'in_play':None},
                 'zero':{'in_play':0},'one':{'in_play':1},'string':{'in_play':'false'},
                 'array':[],'nonobject':'SECRET_SENTINEL','json_null':None}
        if case=='timeout':raise httpx.ReadTimeout('SECRET_SENTINEL',request=request)
        if case=='invalid_json':return httpx.Response(200,content=b'SECRET_SENTINEL',request=request)
        if case=='http_failure':return httpx.Response(503,json={'error':'SECRET_SENTINEL'},request=request)
        return httpx.Response(200,content=json.dumps(replies[case]).encode(),request=request)
    monkeypatch.setattr(SecureClient,'create',lambda **kw:pytest.fail('observational SDK cannot create signing client'))
    public,gets=external_sdk([taker_fee_wire()])
    sdk=ReadOnlySDK(public=public,http=httpx.Client(transport=httpx.MockTransport(http)))
    try:
        if case in ('missing','false'):
            fixed=sdk.read_fixed_market(AT,CID)[0]
            assert fixed['eligible'] is True and fixed['skip_reason'] is None
            assert fixed['plan'].market.condition_id==CID and fixed['plan'].market.in_play is False
            assert tuple(t.token_id for t in fixed['plan'].market.tokens)==('101','102')
        else:
            with pytest.raises(ValueError,match='fixed market validation unavailable'):sdk.read_fixed_market(AT,CID)
        scanned=sdk.read_markets(AT)
        assert len(scanned)==1
        row=scanned[0]
        assert row['eligible'] is (case in ('missing','false'))
        if case in ('missing','false'):
            assert row['plan'] is not None and row['skip_reason'] is None
        else:assert row['plan'] is None and row['skip_reason']
        assert 'SECRET_SENTINEL' not in repr(row)
        assert seen and gets and all(method=='GET' for method,url in gets)
    finally:sdk.close()


def test_builder_readiness_uses_signed_get_without_merge(monkeypatch,caplog):
    import base64,hashlib,hmac,time,json
    from eth_account import Account
    from polymarket.environments import PRODUCTION
    from polymarket._internal.wallet import derive_proxy_wallet_address
    from lp_v2.readonly_sdk import ReadOnlySDK,L2Credentials
    signer=Account.from_key(bytes([7])*32).address
    wallet=derive_proxy_wallet_address(signer,PRODUCTION.wallet_derivation)
    identity=dict(signer_address=signer,wallet_address=wallet,signature_type=1)
    creds=L2Credentials(key='l2-key',secret='c2VjcmV0',passphrase='l2-phrase',**identity)
    builder=dict(kind='builder',key='builder-sentinel',secret='c3ludGhldGljLXNlY3JldA==',passphrase='phrase-sentinel',address=signer)
    monkeypatch.setattr(time,'time',lambda:1700000000.75)
    seen=[];scenario={}
    def http(request):
        seen.append(request)
        assert request.method=='GET'
        path=request.url.path
        assert path in ('/balance-allowance','/auth/ban-status/closed-only','/data/orders','/api/geoblock','/transactions')
        if path=='/transactions':
            assert str(request.url)=='https://relayer-v2.polymarket.com/transactions'
            if scenario.get('legacy'):
                assert request.headers['RELAYER_API_KEY']=='legacy-key'
                assert request.headers['RELAYER_API_KEY_ADDRESS'].lower()==signer.lower()
                assert not any(k.startswith('poly_builder') for k in request.headers)
            else:
                expected=base64.urlsafe_b64encode(hmac.new(b'synthetic-secret',b'1700000000GET/transactions',hashlib.sha256).digest()).decode()
                assert {k:v for k,v in request.headers.items() if k.startswith('poly_builder')}=={
                    'poly_builder_api_key':'builder-sentinel','poly_builder_passphrase':'phrase-sentinel',
                    'poly_builder_timestamp':'1700000000','poly_builder_signature':expected}
                assert 'RELAYER_API_KEY' not in request.headers
            return httpx.Response(scenario.get('status',200),json=scenario.get('rows',[{'owner':signer}]))
        data={'/balance-allowance':dict(balance='200000000',allowances={PRODUCTION.standard_exchange:'200000000',PRODUCTION.neg_risk_exchange:'200000000'}),
              '/auth/ban-status/closed-only':dict(closed_only=False),'/data/orders':dict(data=[],next_cursor='LTE='),'/api/geoblock':dict(blocked=False,country='US')}
        return httpx.Response(200,json=data[path])
    public,_=external_sdk()
    sdk=ReadOnlySDK(public=public,http=httpx.Client(transport=httpx.MockTransport(http)),credentials=creds)
    def check(payload):return sdk.read_account_readiness(AT,capital_usdc=D('100'),identity=identity,relayer=payload)
    try:
        good=check(builder)
        assert good['relayer_auth']['status']=='PASS'
        assert good['relayer_auth']['reason']=='AUTHENTICATED_GET_ONLY'
        assert good['relayer_merge']['status']=='UNKNOWN' and good['relayer_merge']['reason']=='AUTH_DOES_NOT_PROVE_MERGE_READINESS'
        for status,rows,expected in [(401,[], 'FAIL'),(403,[],'FAIL'),(500,[],'UNKNOWN'),(200,{},'UNKNOWN'),(200,[{'owner':signer},{'owner':'0x'+'9'*40}],'UNKNOWN'),(200,[{}],'UNKNOWN')]:
            scenario.update(status=status,rows=rows);before=len([r for r in seen if r.url.path=='/transactions'])
            result=check(builder)
            assert result['relayer_auth']['status']==expected
            assert result['relayer_merge']['status']=='UNKNOWN'
            assert len([r for r in seen if r.url.path=='/transactions'])==before+1
        scenario.clear()
        for payload in ({},dict(builder,extra='x'),dict(builder,secret='!'),dict(builder,address=False),dict(builder,kind='auto'),dict(builder,key='bad\nheader')):
            before=len([r for r in seen if r.url.path=='/transactions'])
            result=check(payload)
            assert result['relayer_auth']['status']=='FAIL'
            assert len([r for r in seen if r.url.path=='/transactions'])==before
            assert 'sentinel' not in json.dumps(result)
        scenario['legacy']=True
        assert check(dict(key='legacy-key',address=signer))['relayer_auth']['status']=='PASS'
        assert 'sentinel' not in caplog.text
    finally:sdk.close()
