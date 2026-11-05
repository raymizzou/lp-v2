from dataclasses import replace
from decimal import Decimal as D
import json
import os
import pytest
from tests.test_live_sdk import CID,AT,WALLET,SIGNER
from lp_v2.cli import parse_run


@pytest.fixture(autouse=True)
def historical_market_clock(monkeypatch):
    from datetime import datetime
    from lp_v2 import readonly_sdk
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return AT
    monkeypatch.setattr(readonly_sdk,'datetime',Clock)


def live_args(tmp_path):
    signing=tmp_path/'signing'
    from eth_account import Account
    from polymarket.environments import PRODUCTION
    from polymarket._internal.wallet import derive_proxy_wallet_address
    synthetic=Account.from_key(bytes([7])*32)
    wallet=derive_proxy_wallet_address(synthetic.address,PRODUCTION.wallet_derivation)
    signing.write_text('0x'+synthetic.key.hex())
    signing.chmod(0o600)
    credentials=tmp_path/'l2.json'
    credentials.write_text(json.dumps(dict(key='synthetic',secret='c3ludGhldGlj',passphrase='synthetic',
        signer_address=synthetic.address,wallet_address=wallet,signature_type=1)))
    credentials.chmod(0o600)
    args=['run','--live','--capital-usdc','100','--market',CID,'--private-key-file',str(signing),
          '--state-dir',str(tmp_path/'state'),'--credentials-file',str(credentials)]
    return args,signing


def test_live_validation_and_secret_boundary(tmp_path,capsys):
    args,signing=live_args(tmp_path)
    config=parse_run(args)
    assert config.live is True and config.market==CID
    assert config.capital_usdc==D('100')
    assert parse_run(['run','--capital-usdc','100']).live is False
    for flag in ('--market','--private-key-file','--state-dir','--credentials-file'):
        index=args.index(flag)
        with pytest.raises(SystemExit):
            parse_run(args[:index]+args[index+2:])
    invalid=args.copy();invalid[invalid.index('--market')+1]='alien'
    with pytest.raises(SystemExit):
        parse_run(invalid)
    signing.chmod(0o644)
    with pytest.raises(SystemExit):
        parse_run(args)
    signing.chmod(0o600)
    link=tmp_path/'link';link.symlink_to(signing)
    invalid=args.copy();invalid[invalid.index('--private-key-file')+1]=str(link)
    with pytest.raises(SystemExit):
        parse_run(invalid)
    assert 'synthetic' not in repr(config)
    assert 'synthetic' not in str(capsys.readouterr())


def test_stop_signals_owned_process(tmp_path,monkeypatch):
    from lp_v2.live_process import ProcessLock,stop_process
    import lp_v2.live_process as process
    signals=[]
    monkeypatch.setattr(process,'process_identity',lambda pid:'boot:123' if pid==os.getpid() else None)
    monkeypatch.setattr(os,'pidfd_open',lambda pid,flags=0:99,raising=False)
    monkeypatch.setattr(process.signal,'pidfd_send_signal',lambda fd,sig,*args:signals.append((fd,sig)),raising=False)
    original_close=os.close
    monkeypatch.setattr(os,'close',lambda fd:None if fd==99 else original_close(fd))
    state=tmp_path/'state'
    with ProcessLock(state) as lease:
        with pytest.raises(BlockingIOError):
            with ProcessLock(state):
                pass
        tick=[0]
        def wait(seconds):
            tick[0]+=seconds
            lease.finish(0)
        assert stop_process(state,monotonic=lambda:tick[0],wait=wait)==0
        assert len(signals)==1
        metadata=json.loads((state/'process.json').read_text())
        metadata['identity']='stale'
        (state/'process.json').write_text(json.dumps(metadata))
        assert stop_process(state)==1
        assert len(signals)==1
        metadata['identity']='boot:123';metadata['pid']=9999
        (state/'process.json').write_text(json.dumps(metadata))
        assert stop_process(state)==1
        assert len(signals)==1
    assert stop_process(state)==0
    assert stop_process(tmp_path/'missing')==1
    from lp_v2.cli import main
    assert main(['stop','--state-dir',str(tmp_path/'missing')])==1


@pytest.mark.parametrize('unresolved',[False,True])
def test_live_server_lifecycle(tmp_path,monkeypatch,unresolved):
    from threading import Event,enumerate as threads
    from time import monotonic,sleep
    import signal
    from lp_v2.status_server import serve
    from lp_v2 import status_server
    from tests.test_live_runtime import world
    from tests.test_status_server import request
    runner,ex,live,journal,obs=world(tmp_path)
    runner.config=replace(runner.config,live=True)
    runner.stop_timeout_s=.05
    ready=Event()
    original=ex.sdk.post_order.side_effect
    def post(signed):
        if unresolved:
            ready.set()
            raise TimeoutError('unknown')
        result=original(signed)
        if len(ex.posts)==2:
            ready.set()
        return result
    ex.sdk.post_order.side_effect=post
    events=[]
    class Server:
        def __init__(self,address,handler):
            assert address==('127.0.0.1',8765)
            self.handler=handler
        def __enter__(self):
            return self
        def handle_request(self):
            assert ready.wait(2)
            deadline=monotonic()+2
            while runner.snapshot()['as_of']=='UNKNOWN' and monotonic()<deadline:
                sleep(.001)
            body=request(self.handler)[2]
            assert body['mode']=='live' and body['as_of']!='UNKNOWN'
            assert request(self.handler,path='/healthz')[2]['capital_usdc']=='100'
            assert request(self.handler,method='POST')[0]==405
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM,None)
            events.append('signal')
        def __exit__(self,*args):
            events.append('closed')
    monkeypatch.setattr(status_server,'HTTPServer',Server)
    assert serve(runner.config,runner=runner)==(1 if unresolved else 0)
    assert events==['signal','closed']
    assert not any(t.name=='lp-v2-live' for t in threads())
    assert ex.sdk.close.call_count==1
    # Journal created on caller thread was actually used by the production thread.
    import sqlite3
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='stop_result'").fetchone()[0]==1


def test_live_factory_uses_pinned_sdk_and_frozen_identity(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import httpx
    from lp_v2.live_runtime import create_live_runner
    from polymarket import SecureClient
    from tests.test_readonly_sdk import external_sdk,market_wire,CID as market_id
    from tests.fakes.live_exchange import Exchange
    args,_=live_args(tmp_path)
    args[args.index('--market')+1]=market_id
    relay=tmp_path/'relayer.json'
    relay.write_text(json.dumps(dict(key='synthetic-relayer',address=json.loads((tmp_path/'l2.json').read_text())['signer_address'])))
    relay.chmod(0o600)
    args+=['--relayer-credentials-file',str(relay)]
    config=replace(parse_run(args),started_at=AT)
    (tmp_path/'state').mkdir(mode=0o700)
    ex=Exchange()
    ex.sdk.wallet_type='POLY_PROXY';ex.sdk.wallet=config.credentials.wallet_address;ex.sdk.signer=config.credentials.signer_address
    object.__setattr__(ex.sdk,'_ctx',SimpleNamespace(rpc=None,relayer=None))
    seen=[]
    def create(**kwargs):
        seen.append(kwargs)
        assert kwargs['credentials'].key=='synthetic'
        assert kwargs['api_key'].key=='synthetic-relayer'
        assert kwargs['wallet']==config.credentials.wallet_address
        return ex.sdk
    monkeypatch.setattr(SecureClient,'create',create)
    public,_=external_sdk(rows=[market_wire()])
    http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'in_play':False,'blocked':False,'country':'US'})))
    runner=create_live_runner(config,public=public,http=http,clock=lambda:AT)
    assert runner.condition_id==market_id and runner.sdk.token_ids==('101','102')
    assert len(seen)==1
    runner.sdk.close();runner.observations.close()


@pytest.mark.parametrize('invalid',['key','signer','wallet','deposit','type'])
def test_factory_rejects_identity_before_sdk_wallet_setup(tmp_path,monkeypatch,invalid):
    from lp_v2.live_runtime import create_live_runner
    from polymarket import SecureClient
    from eth_account import Account
    from polymarket.environments import PRODUCTION
    from polymarket._internal.wallet import derive_beacon_deposit_wallet_address
    args,signing=live_args(tmp_path)
    relay=tmp_path/'relayer.json'
    relay.write_text(json.dumps(dict(key='synthetic-relayer',address=SIGNER)));relay.chmod(0o600)
    args+=['--relayer-credentials-file',str(relay)]
    config=replace(parse_run(args),started_at=AT)
    c=config.credentials
    if invalid=='key':
        signing.write_text('invalid signing material')
    elif invalid=='signer':
        config=replace(config,credentials=replace(c,signer_address=SIGNER))
    elif invalid=='wallet':
        config=replace(config,credentials=replace(c,wallet_address=WALLET))
    elif invalid=='deposit':
        config=replace(config,credentials=replace(c,wallet_address=derive_beacon_deposit_wallet_address(c.signer_address,PRODUCTION.wallet_derivation)))
    else:
        config=replace(config,credentials=replace(c,signature_type=0))
    constructed=[]
    def create(**kw):
        constructed.append(kw)
        raise AssertionError('wallet constructor must not be reached')
    monkeypatch.setattr(SecureClient,'create',create)
    import httpx
    from tests.test_readonly_sdk import external_sdk,market_wire,CID as market_id
    public,_=external_sdk(rows=[market_wire()])
    http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'in_play':False})))
    with pytest.raises(ValueError,match='identity|signing'):
        create_live_runner(replace(config,market=market_id),public=public,http=http,clock=lambda:AT)
    assert constructed==[]


def test_stop_after_executor_exit_is_idempotent(tmp_path,monkeypatch):
    from lp_v2.live_process import ProcessLock,stop_process
    import lp_v2.live_process as process
    monkeypatch.setattr(process,'process_identity',lambda pid:'boot:123' if pid==os.getpid() else None)
    signals=[]
    monkeypatch.setattr(os,'pidfd_open',lambda *args:signals.append('opened'),raising=False)
    state=tmp_path/'state'
    with ProcessLock(state) as lease:
        lease.finish(1)
        before=(state/'stop-result.json').read_bytes()
        metadata=json.loads((state/'process.json').read_text())
        for invalid in (dict(metadata,identity='stale'),dict(metadata,pid=9999)):
            (state/'process.json').write_text(json.dumps(invalid))
            assert stop_process(state)==1
        (state/'process.json').write_text('{')
        assert stop_process(state)==1
        (state/'process.json').write_text(json.dumps(metadata))
    assert stop_process(state)==0
    assert stop_process(state)==0
    assert (state/'stop-result.json').read_bytes()==before
    assert signals==[]
    assert stop_process(tmp_path/'missing')==1
    (state/'executor.lock').chmod(0o644)
    assert stop_process(state)==1
    assert signals==[]


def public_preflight_world(monkeypatch, wire, *, unavailable=False, stale=False):
    import httpx
    from datetime import datetime
    from lp_v2 import cli
    from tests.test_readonly_sdk import external_sdk, AT as public_at
    public, seen = external_sdk([wire])
    route = public._ctx.gamma._client._transport.handle_request
    def dispatch(self, request):
        if unavailable:
            raise httpx.ConnectError('synthetic-credential-sentinel', request=request)
        response = route(request)
        if stale and request.url.path == '/book':
            response.read()
            data = response.json(); data['timestamp'] = '1'
            return httpx.Response(200,json=data)
        return response
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', dispatch)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return public_at
    monkeypatch.setattr(cli, 'datetime', Clock)
    from lp_v2 import readonly_sdk
    monkeypatch.setattr(readonly_sdk, 'datetime', Clock)
    return seen, public


@pytest.mark.parametrize('case', ['pass','blocked','unknown'])
def test_preflight_is_public_and_observational(tmp_path,monkeypatch,capsys,case):
    from lp_v2.cli import main
    from tests.test_readonly_sdk import taker_fee_wire,CID as market_id
    import lp_v2.cli as cli
    from polymarket import SecureClient
    from lp_v2.journal import Journal
    from lp_v2.live_process import ProcessLock
    import subprocess
    wire=taker_fee_wire()
    if case=='blocked': wire['feeSchedule']['takerOnly']=False
    seen, public=public_preflight_world(monkeypatch,wire,unavailable=case=='unknown')
    def forbidden(*a,**kw): raise AssertionError('private or mutation boundary reached')
    monkeypatch.setattr(cli,'owner_file',forbidden)
    monkeypatch.setattr(SecureClient,'create',forbidden)
    monkeypatch.setattr(Journal,'__init__',forbidden)
    monkeypatch.setattr(ProcessLock,'__init__',forbidden)
    monkeypatch.setattr(subprocess,'Popen',forbidden)
    monkeypatch.chdir(tmp_path)
    before=set(tmp_path.rglob('*'))
    try:
        result=main(['preflight','--market',market_id,'--capital-usdc','100','--code-sha','a'*40])
        body=json.loads(capsys.readouterr().out)
        assert result==(0 if case=='pass' else 2)
        assert body['status']=={'pass':'PASS','blocked':'BLOCKED','unknown':'UNKNOWN'}[case]
        assert body['scope']=='public_market' and body['market']==market_id
        assert body['code_sha']=='a'*40 and body['capital_usdc']=='100' and body['as_of']
        for key in ('private_identity','relayer','allowance'):
            assert body['checks'][key]['status']=='NOT_CHECKED'
        assert 'synthetic-credential-sentinel' not in json.dumps(body)
        assert all(method=='GET' for method,_ in seen)
        assert set(tmp_path.rglob('*'))==before
    finally: public.close()


@pytest.mark.parametrize('case',['independent-blockers','foreign-identity'])
def test_preflight_reports_all_available_blockers(tmp_path,monkeypatch,capsys,case):
    from lp_v2.cli import main
    from tests.test_readonly_sdk import taker_fee_wire,CID as market_id,BAD
    wire=taker_fee_wire()
    if case=='independent-blockers':
        wire['feeSchedule']['takerOnly']=False
        wire['acceptingOrders']=False
    else:
        wire['conditionId']=BAD
        wire['clobTokenIds']=['901','902']
    seen,public=public_preflight_world(monkeypatch,wire,stale=True)
    try:
        assert main(['preflight','--market',market_id,'--capital-usdc','100','--code-sha','a'*40])==2
        body=json.loads(capsys.readouterr().out)
        checks=body['checks']
        if case=='independent-blockers':
            assert all(checks[k]['status']=='BLOCKED' for k in ('fees','state','books'))
            assert checks['market_identity']['status']=='PASS'
        else:
            assert checks['market_identity']['status']=='BLOCKED'
            assert all(checks[k]['status']=='UNKNOWN' for k in ('fees','state','books','token_identity','in_play'))
            assert not any('/book' in url or BAD in url or '901' in url or '902' in url for _,url in seen)
    finally: public.close()


def test_live_startup_reports_safe_market_failure(tmp_path,monkeypatch,capsys):
    from lp_v2.cli import main
    from tests.test_readonly_sdk import unproven_fee_wire,CID as market_id
    from polymarket import SecureClient
    import lp_v2.live_process as process
    args,_=live_args(tmp_path)
    args[args.index('--market')+1]=market_id
    relay=tmp_path/'relayer.json'
    relay.write_text(json.dumps(dict(key='synthetic-credential-sentinel',address=SIGNER)));relay.chmod(0o600)
    args+=['--relayer-credentials-file',str(relay)]
    seen,public=public_preflight_world(monkeypatch,unproven_fee_wire('non-taker-only'))
    monkeypatch.setattr(process,'process_identity',lambda pid:'boot:123')
    constructed=[]
    def forbidden(**kw):
        constructed.append(kw)
        raise AssertionError('SecureClient must not be constructed')
    monkeypatch.setattr(SecureClient,'create',forbidden)
    try:
        assert main(args)!=0
        output=capsys.readouterr()
        assert 'stage=market_check' in output.out
        assert 'reason_code=MARKET_VALIDATION_UNAVAILABLE' in output.out
        assert 'market='+market_id in output.out
        assert 'synthetic-credential-sentinel' not in output.out+output.err
        assert constructed==[] and all(m=='GET' for m,_ in seen)
        assert not (tmp_path/'state'/'journal.sqlite').exists()
    finally: public.close()


def test_preflight_explains_liquidity_threshold(monkeypatch,capsys):
    import httpx
    from lp_v2.cli import main
    from tests.test_readonly_sdk import taker_fee_wire,CID,AT
    for liquidity,prices,expected,blockers in [
        ('825.3674',('0.36','0.37'),'BLOCKED',['LIQUIDITY_BELOW_MIN']),
        ('999.9999',('0.36','0.37'),'BLOCKED',['LIQUIDITY_BELOW_MIN']),
        ('1000',('0.36','0.37'),'PASS',[]),
        ('1000',('0.92','0.94'),'BLOCKED',['MIDPOINT_OUTSIDE_RANGE']),
        (True,('0.36','0.37'),'BLOCKED',[]),
        ('NaN',('0.36','0.37'),'BLOCKED',[]),
        (None,('0.36','0.37'),'BLOCKED',[])]:
        with monkeypatch.context() as patch:
            wire=taker_fee_wire();wire['liquidityNum']=liquidity
            _,public=public_preflight_world(patch,wire)
            route=httpx.HTTPTransport.handle_request
            def http(transport,request):
                response=route(transport,request)
                if request.url.path=='/book' and request.url.params['token_id']=='101':
                    response.read();data=response.json()
                    data['bids'][0]['price'],data['asks'][0]['price']=prices
                    return httpx.Response(200,json=data)
                return response
            patch.setattr(httpx.HTTPTransport,'handle_request',http)
            try:
                code=main(['preflight','--market',CID,'--capital-usdc','100','--code-sha','a'*40])
                report=json.loads(capsys.readouterr().out);row=report['checks']['opening_eligibility']
                assert row['status']==expected and code==(0 if expected=='PASS' else 2)
                details=row['details']
                assert details['minimum_liquidity']=='1000'
                assert details['minimum_midpoint']=='0.10' and details['maximum_midpoint']=='0.90'
                assert details['as_of']==AT.isoformat() and details['blockers']==blockers
                assert details['midpoint']==('0.93' if prices[0]=='0.92' else '0.365')
                if liquidity in (True,'NaN',None):assert 'liquidity_num' not in details
                else:assert details['liquidity_num']==liquidity
            finally:public.close()


def in_play_http_response(request,case):
    """Literal external CLOB replies; no internal policy decisions are faked."""
    import httpx
    data={'missing':{},'false':{'in_play':False},'true':{'in_play':True},
          'null':{'in_play':None},'zero':{'in_play':0},'one':{'in_play':1},
          'string':{'in_play':'false'},'array':[],'nonobject':'SECRET_SENTINEL','json_null':None}
    if case=='timeout':raise httpx.ReadTimeout('SECRET_SENTINEL',request=request)
    if case=='invalid_json':return httpx.Response(200,content=b'SECRET_SENTINEL',request=request)
    if case=='http_failure':return httpx.Response(503,json={'error':'SECRET_SENTINEL'},request=request)
    return httpx.Response(200,content=json.dumps(data[case]).encode(),request=request)


@pytest.mark.parametrize('case',['missing','false','true','null','zero','one','string','array','nonobject','json_null','invalid_json','http_failure','timeout'])
def test_preflight_accepts_false_or_absent_in_play(monkeypatch,capsys,case):
    import httpx,subprocess
    from polymarket import SecureClient
    from lp_v2.cli import main
    from tests.test_readonly_sdk import taker_fee_wire,CID,AT
    _,public=public_preflight_world(monkeypatch,taker_fee_wire())
    route=httpx.HTTPTransport.handle_request;seen=[]
    def http(transport,request):
        seen.append(request)
        assert request.method=='GET'
        assert request.url.path in ('/markets/keyset','/markets/'+CID,'/book')
        if request.url.path=='/markets/'+CID:return in_play_http_response(request,case)
        return route(transport,request)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',http)
    monkeypatch.setattr(SecureClient,'create',lambda **kw:pytest.fail('public preflight cannot create signing client'))
    monkeypatch.setattr(subprocess,'run',lambda *args,**kw:pytest.fail('public preflight cannot mutate a service'))
    try:
        code=main(['preflight','--market',CID,'--capital-usdc','100','--code-sha','a'*40])
        report=json.loads(capsys.readouterr().out)
        allowed=case in ('missing','false')
        expected='PASS' if allowed else 'UNKNOWN' if case in ('array','nonobject','json_null','invalid_json','http_failure','timeout') else 'BLOCKED'
        assert report['checks']['in_play']['status']==expected and report['status']==expected
        assert code==(0 if allowed else 2)
        assert report['market']==CID and report['code_sha']=='a'*40 and report['capital_usdc']=='100' and report['as_of']==AT.isoformat()
        assert all(report['checks'][k]['status']=='NOT_CHECKED' for k in ('private_identity','relayer','allowance'))
        assert 'SECRET_SENTINEL' not in json.dumps(report) and seen
    finally:public.close()


@pytest.mark.parametrize('case',['missing','false','true','null','http_failure','timeout'])
def test_live_factory_accepts_absent_in_play(tmp_path,monkeypatch,case):
    from types import SimpleNamespace
    import httpx
    from lp_v2.live_runtime import create_live_runner,MarketCheckFailure
    from polymarket import SecureClient
    from tests.test_readonly_sdk import external_sdk,taker_fee_wire,CID,AT
    from tests.fakes.live_exchange import Exchange
    args,_=live_args(tmp_path);args[args.index('--market')+1]=CID
    config=parse_run(args)
    relay=tmp_path/'relayer.json'
    relay.write_text(json.dumps(dict(key='synthetic-relayer',address=config.credentials.signer_address)));relay.chmod(0o600)
    config=replace(config,started_at=AT,relayer_credentials_file=str(relay))
    from pathlib import Path
    Path(config.state_dir).mkdir(mode=0o700)
    ex=Exchange();ex.sdk.wallet_type='POLY_PROXY';ex.sdk.wallet=config.credentials.wallet_address;ex.sdk.signer=config.credentials.signer_address
    object.__setattr__(ex.sdk,'_ctx',SimpleNamespace(rpc=None,relayer=None))
    created=[]
    def create(**kwargs):
        created.append(kwargs)
        assert kwargs['wallet']==config.credentials.wallet_address
        assert kwargs['credentials'].key=='synthetic' and kwargs['api_key'].key=='synthetic-relayer'
        return ex.sdk
    monkeypatch.setattr(SecureClient,'create',create)
    public,_=external_sdk([taker_fee_wire()])
    http=httpx.Client(transport=httpx.MockTransport(lambda request:in_play_http_response(request,case)))
    if case in ('missing','false'):
        runner=create_live_runner(config,public=public,http=http,clock=lambda:AT)
        try:
            assert len(created)==1 and runner.config==config
            assert runner.condition_id==CID and runner.sdk.condition_id==CID and runner.sdk.token_ids==('101','102')
            assert runner.sdk.wallet==config.credentials.wallet_address and runner.sdk.signer==config.credentials.signer_address
            assert runner.sdk.owner=='lp-v2:'+config.credentials.wallet_address.lower()
            assert ex.posts==[]
            ex.sdk.create_limit_order.assert_not_called();ex.sdk.cancel_order.assert_not_called()
        finally:runner.sdk.close();runner.observations.close()
    else:
        with pytest.raises(MarketCheckFailure):create_live_runner(config,public=public,http=http,clock=lambda:AT)
        assert created==[] and not (Path(config.state_dir)/'journal.sqlite').exists()


def test_live_factory_selects_explicit_builder_credentials(tmp_path,monkeypatch,capsys):
    from types import SimpleNamespace
    import httpx
    from polymarket import SecureClient
    from polymarket.auth import BuilderApiKey,RelayerApiKey
    from lp_v2.live_runtime import create_live_runner
    from tests.test_readonly_sdk import external_sdk,market_wire,CID as market_id
    from tests.fakes.live_exchange import Exchange
    args,_=live_args(tmp_path)
    args[args.index('--market')+1]=market_id
    relay=tmp_path/'relayer.json';relay.touch(mode=0o600)
    args+=['--relayer-credentials-file',str(relay)]
    config=replace(parse_run(args),started_at=AT)
    signer=config.credentials.signer_address
    builder=dict(kind='builder',key='builder-sentinel',secret='c3ludGhldGljLXNlY3JldA==',passphrase='phrase-sentinel',address=signer)
    seen=[]
    def create(**kw):
        seen.append(kw)
        ex=Exchange();ex.sdk.wallet_type='POLY_PROXY';ex.sdk.wallet=config.credentials.wallet_address;ex.sdk.signer=signer
        object.__setattr__(ex.sdk,'_ctx',SimpleNamespace(rpc=None,relayer=None))
        return ex.sdk
    monkeypatch.setattr(SecureClient,'create',create)
    for n,payload in enumerate([builder,dict(key='legacy-key',address=signer),dict(builder,address=signer.lower(),secret=builder['secret'].rstrip('='))]):
        relay.write_text(json.dumps(payload))
        state=tmp_path/f'valid-{n}';state.mkdir(mode=0o700)
        public,_=external_sdk([market_wire()])
        http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'in_play':False})))
        runner=create_live_runner(replace(config,state_dir=str(state)),public=public,http=http,clock=lambda:AT)
        key=seen[-1]['api_key']
        assert type(key) is (BuilderApiKey if n!=1 else RelayerApiKey)
        assert key.key==payload['key']
        if n!=1:assert (key.secret,key.passphrase)==(payload['secret'],'phrase-sentinel')
        else:assert str(key.address).lower()==signer.lower()
        assert seen[-1]['wallet']==config.credentials.wallet_address
        runner.sdk.close();runner.observations.close()
    assert len(seen)==3
    invalid=[{},None,[],dict(builder,extra='x'),dict(builder,kind='auto'),dict(builder,address='0x1234'),
             dict(builder,address='0x'+'9'*40),dict(builder,key=True),dict(builder,secret=None),
             dict(builder,secret='!invalid!'),dict(builder,secret='a'),dict(builder,secret='===='),
             dict(builder,passphrase=' \t'),dict(builder,key='bad\rheader'),dict(builder,passphrase='bad\x00header'),
             dict(builder,secret='é'),dict(key='legacy-key',address='0x'+'9'*40),dict(key='',address=signer)]
    invalid += [{k:v for k,v in builder.items() if k!=missing} for missing in builder]
    for payload in invalid:
        relay.write_text(json.dumps(payload))
        state=tmp_path/'rejected'
        public,_=external_sdk([market_wire()])
        http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'in_play':False})))
        with pytest.raises(ValueError,match='^invalid relayer credentials$') as error:
            create_live_runner(replace(config,state_dir=str(state)),public=public,http=http,clock=lambda:AT)
        assert 'sentinel' not in str(error.value)
        assert not state.exists() and len(seen)==3
    assert 'sentinel' not in str(capsys.readouterr())


def timed_preflight_world(patch, case='normal'):
    """External UTC and literal token-keyed HTTP timeline, independent of app policy."""
    from datetime import datetime, timedelta
    import httpx
    from lp_v2 import cli, readonly_sdk
    from tests.test_readonly_sdk import taker_fee_wire, AT
    seen, public = public_preflight_world(patch, taker_fee_wire())
    current = [AT]
    group_started = [AT]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return current[0]
    patch.setattr(cli, 'datetime', Clock)
    patch.setattr(readonly_sdk, 'datetime', Clock, raising=False)
    route = httpx.HTTPTransport.handle_request
    def handle(transport, request):
        if request.url.path=='/api/geoblock':
            return httpx.Response(200,json=dict(blocked=False,country='US'))
        response = route(transport, request)
        if request.url.path == '/book':
            token = request.url.params['token_id']
            if token=='101':group_started[0]=current[0]
            started=group_started[0]
            receipt = {'101':2, '102':3}
            stamp = {'101':1, '102':1}
            if case=='future-receipt': stamp['101']=3; receipt['102']=4
            if case=='aged-first': stamp={'101':0,'102':60}; receipt={'101':1,'102':61}
            if case=='exact60': stamp={'101':0,'102':0}; receipt={'101':1,'102':60}
            if case=='stale': stamp={'101':-61,'102':-61}
            if case=='backward': receipt={'101':2,'102':1}; stamp={'101':0,'102':0}
            current[0]=started+timedelta(seconds=receipt[token])
            if case=='naive-clock': current[0]=current[0].replace(tzinfo=None)
            if case=='failed': raise httpx.ReadTimeout('synthetic',request=request)
            response.read(); data=response.json()
            data['timestamp']=str(int((started+timedelta(seconds=stamp[token])).timestamp()*1000))
            if case=='missing':data.pop('timestamp')
            if case=='invalid':data['timestamp']='invalid'
            if case=='naive-book':data['timestamp']='2026-11-02T00:00:01'
            return httpx.Response(200,json=data)
        return response
    patch.setattr(httpx.HTTPTransport, 'handle_request', handle)
    return public, seen, current


@pytest.mark.parametrize('case', ['normal','future-receipt','aged-first','exact60','stale','backward','naive-clock','failed','missing','invalid','naive-book'])
def test_preflight_uses_completed_book_observation_time(monkeypatch,capsys,case):
    from lp_v2.cli import main
    from tests.test_readonly_sdk import CID, AT
    from datetime import timedelta
    public,seen,current=timed_preflight_world(monkeypatch,case)
    try:
        code=main(['preflight','--market',CID,'--capital-usdc','100','--code-sha','a'*40])
        report=json.loads(capsys.readouterr().out)
        valid=case in ('normal','exact60')
        assert (code==0) is valid
        assert (report['checks']['books']['status']=='PASS') is valid
        if valid:
            expected=AT+timedelta(seconds=3 if case=='normal' else 60)
            assert report['as_of']==expected.isoformat()
            assert report['checks']['opening_eligibility']['details']['as_of']==expected.isoformat()
        assert all(method=='GET' for method,_ in seen)
    finally:public.close()


@pytest.mark.parametrize('case',['normal','future-receipt','aged-first'])
def test_factory_accepts_book_created_during_preflight(tmp_path,monkeypatch,case):
    from pathlib import Path
    from types import SimpleNamespace
    from polymarket import SecureClient,PublicClient
    from tests.test_readonly_sdk import CID,AT
    from tests.fakes.live_exchange import Exchange
    from lp_v2.live_runtime import create_live_runner,MarketCheckFailure
    import httpx
    public,seen,current=timed_preflight_world(monkeypatch,case)
    args,_=live_args(tmp_path);args[args.index('--market')+1]=CID
    config=parse_run(args)
    relay=tmp_path/'relayer.json'
    relay.write_text(json.dumps(dict(key='synthetic-relayer',address=config.credentials.signer_address)));relay.chmod(0o600)
    config=replace(config,started_at=AT,relayer_credentials_file=str(relay))
    ex=Exchange(condition_id=CID);ex.sdk.wallet_type='POLY_PROXY';ex.sdk.wallet=config.credentials.wallet_address;ex.sdk.signer=config.credentials.signer_address
    object.__setattr__(ex.sdk,'_ctx',SimpleNamespace(rpc=None,relayer=None))
    created=[]
    def create(**kw):
        created.append(kw)
        assert kw['wallet']==config.credentials.wallet_address and kw['credentials'].key=='synthetic'
        return ex.sdk
    monkeypatch.setattr(SecureClient,'create',create)
    try:
        if case=='normal':
            Path(config.state_dir).mkdir(mode=0o700)
            runner=create_live_runner(config,public=PublicClient(),http=httpx.Client(),clock=lambda:current[0])
            assert len(created)==1 and runner.sdk.token_ids==('101','102') and runner.config.started_at==AT
            assert runner.sdk.wallet==config.credentials.wallet_address and runner.sdk.signer==config.credentials.signer_address
            runner.sdk.close();runner.observations.close()
        else:
            with pytest.raises(MarketCheckFailure):create_live_runner(config,public=PublicClient(),http=httpx.Client(),clock=lambda:current[0])
            assert created==[] and not (Path(config.state_dir)/'journal.sqlite').exists()
        assert ex.posts==[] and all(method=='GET' for method,_ in seen)
    finally:public.close()
