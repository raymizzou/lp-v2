"""Operator CLI with external SSH/systemd/HTTP boundaries replaced."""
import copy
from datetime import datetime, UTC, timedelta
import json
import subprocess
import pytest
from lp_v2.cli import main
from tests.test_live_control_profiles import profile, config_home, known_ssh
from tests.test_live_control_host import target, combined_world, operator_os

PUBLIC=('market_identity','token_identity','fees','state','political','in_play','rewards','constraints','books','opening_eligibility','market_inputs')
REQUIRED=('release','artifacts','files','private_identity','run_env','state_identity','single_executor','port','service','balance','allowance','closed_only','legacy_orders','region','relayer_auth','proxy_execution','operator_authority','legacy_service','inventory_handoff')


def ready_report(profile):
    stamp=datetime.now(UTC).isoformat()
    return dict(schema_version=1,status='PASS',as_of=stamp,
        checks={key:dict(status='PASS',as_of=stamp) for key in REQUIRED},
        public_market=dict(scope='public_market',code_sha=profile['release']['expected_sha'],
            market=profile['trading']['market_condition_id'],capital_usdc=profile['trading']['capital_usdc'],as_of=stamp,
            status='PASS',checks={key:dict(status='PASS') for key in PUBLIC}))


def install_ssh(monkeypatch,handler):
    def boundary(argv,**kw):
        if known_ssh(argv): return known_ssh(argv)
        result=handler(json.loads(kw['input']))
        return subprocess.CompletedProcess(argv,0,json.dumps(result),'')
    monkeypatch.setattr(subprocess,'run',boundary)


def test_check_reports_independent_blockers(config_home,profile,monkeypatch,capsys):
    data=ready_report(profile)
    data['checks']['release']['status']='FAIL'; data['checks']['files']['status']='FAIL'
    data['checks']['private_identity']['status']='UNKNOWN'
    data['public_market']['status']='BLOCKED'; data['public_market']['checks']['state']['status']='BLOCKED'
    calls=[]
    def handler(req):
        calls.append(req['action']); return data
    install_ssh(monkeypatch,handler)
    assert main(['live','check'])==2
    report=json.loads(capsys.readouterr().out)
    assert report['checks']['release']['status']=='FAIL'
    assert report['checks']['files']['status']=='FAIL'
    assert report['checks']['public_market']['status']=='FAIL'
    assert report['checks']['private_identity']['status']=='UNKNOWN'
    assert calls==['check']


def test_missing_preflight_capability_is_not_pass(config_home,profile,monkeypatch,capsys):
    from tests import test_readonly_sdk as public_fixtures
    from tests.test_live_cli import public_preflight_world
    scenarios=[None,{},dict(scope='private',status='PASS')]
    for field,value in [('code_sha','b'*40),('market','0x'+'9'*64),('capital_usdc','99'),('as_of','2000-01-01T00:00:00+00:00'),('checks',{})]:
        item=ready_report(profile)['public_market']; item[field]=value; scenarios.append(item)
    item=ready_report(profile)['public_market']; del item['checks']['fees']; scenarios.append(item)
    for data in scenarios:
        report=ready_report(profile); report['public_market']=data
        calls=[]
        def handler(req): calls.append(req['action']); return report
        install_ssh(monkeypatch,handler)
        assert main(['live','check'])==2
        result=json.loads(capsys.readouterr().out)
        assert result['checks']['public_market']['status']=='UNKNOWN'
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        capsys.readouterr()
        assert 'on' not in calls
    # Consume actual integrated #14 CLI output, with only external HTTP/SSH faked.
    for case,public_status,control_code in (
            ('pass','PASS',0),('blocked','BLOCKED',2),('unknown','UNKNOWN',2)):
        with monkeypatch.context() as patch:
            patch.setattr(public_fixtures,'AT',datetime.now(UTC))
            wire=public_fixtures.taker_fee_wire()
            if case=='blocked': wire['feeSchedule']['takerOnly']=False
            _,public=public_preflight_world(patch,wire,unavailable=case=='unknown')
            import httpx
            dispatch=httpx.HTTPTransport.handle_request
            def timely_book(transport,request):
                response=dispatch(transport,request)
                if request.url.path=='/book':
                    response.read(); data=response.json()
                    data['timestamp']=str(int((public_fixtures.AT-timedelta(seconds=1)).timestamp()*1000))
                    return httpx.Response(200,json=data)
                return response
            patch.setattr(httpx.HTTPTransport,'handle_request',timely_book)
            try:
                assert main(['preflight','--market',profile['trading']['market_condition_id'],
                    '--capital-usdc',profile['trading']['capital_usdc'],
                    '--code-sha',profile['release']['expected_sha']])==(0 if case=='pass' else 2)
                actual=json.loads(capsys.readouterr().out)
                assert actual['status']==public_status and actual['scope']=='public_market'
                report=ready_report(profile); report['public_market']=actual
                observed=[]
                def target(request):
                    observed.append(request['action']); return report
                install_ssh(patch,target)
                assert main(['live','check'])==control_code
                consumed=json.loads(capsys.readouterr().out)
                assert consumed['checks']['public_market']['status']=={
                    'pass':'PASS','blocked':'FAIL','unknown':'UNKNOWN'}[case]
                assert observed==['check']
            finally:
                public.close()


def test_on_requires_current_confirmation(config_home,profile,monkeypatch,capsys):
    for scenario in ('missing','expired','profile_change','identity_change'):
        calls=[]; data=ready_report(profile)
        def handler(req): calls.append(req['action']); return data
        install_ssh(monkeypatch,handler)
        path=config_home/'profiles/tokyo.json'; path.write_text(json.dumps(profile))
        def confirm(prompt):
            if scenario=='profile_change':
                changed=copy.deepcopy(profile); changed['revision']=2; changed['trading']['capital_usdc']='80'; path.write_text(json.dumps(changed))
            if scenario=='identity_change':
                data['checks']['private_identity']['status']='FAIL'
            return '' if scenario=='missing' else 'on'
        monkeypatch.setattr('builtins.input',confirm)
        end='2000-01-01T00:00:00+00:00' if scenario=='expired' else '2099-01-01T00:00:00+00:00'
        assert main(['live','on','--window-end',end])==2
        assert 'on' not in calls
        assert not (config_home/'operations').exists() or not list((config_home/'operations').glob('*.json'))
        assert any(word in capsys.readouterr().out for word in ('CONFIRMATION','CHANGED','FAIL'))


def live_snapshot(profile,run_id,at):
    return dict(schema_version=1,run_id=run_id,executor_running=True,readonly_stopped=True,identity=profile['identity'],
        summary=dict(mode='live',as_of=at,capital_usdc=profile['trading']['capital_usdc'],
            run=dict(code_sha=profile['release']['expected_sha'],wallet_tail=profile['identity']['wallet_address'][-4:]),
            accounting=dict(status='SUCCESS',as_of=at),freshness=dict(decision_as_of=at),
            markets=[dict(condition_id=profile['trading']['market_condition_id'],eligible=True,skip_reason=None)]))


def test_on_starts_once_and_verifies_two_fresh_rounds(config_home,profile,monkeypatch,capsys):
    calls=[]; now=datetime.now(UTC); rounds=iter([now-timedelta(seconds=2),now-timedelta(seconds=1)])
    def handler(req):
        calls.append(req['action'])
        if req['action']=='check': return ready_report(profile)
        if req['action']=='on': return dict(status='ACCEPTED',run_id='f'*32,started_at=(now-timedelta(seconds=3)).isoformat())
        if req['action']=='observe': return live_snapshot(profile,'f'*32,next(rounds).isoformat())
        pytest.fail('unexpected action')
    install_ssh(monkeypatch,handler); monkeypatch.setattr('builtins.input',lambda _: 'on')
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==0
    result=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result['status']=='PASS' and result['run_id']=='f'*32 and result['verified_rounds']==2
    assert calls==['check','check','on','observe','observe']
    frozen=json.loads((config_home/'runs/tokyo.json').read_text())
    assert frozen['profile']==profile and frozen['run_id']=='f'*32


def test_on_does_not_accept_process_or_http_success_alone(config_home,profile,monkeypatch,capsys):
    import time
    monkeypatch.setattr(time,'sleep',lambda _:None)
    monkeypatch.setattr('builtins.input',lambda _:'on')
    for scenario in ('exit','readonly','stale','accounting'):
        calls=[]; now=datetime.now(UTC); snap=live_snapshot(profile,'f'*32,now.isoformat())
        if scenario=='exit': snap['executor_running']=False
        if scenario=='readonly': snap['summary']['mode']='read_only'
        if scenario=='stale': snap['summary']['as_of']='2000-01-01T00:00:00+00:00'
        if scenario=='accounting': snap['summary']['accounting']['status']='UNKNOWN'
        def handler(req):
            calls.append(req['action'])
            if req['action']=='check': return ready_report(profile)
            if req['action']=='on': return dict(status='ACCEPTED',run_id='f'*32,started_at=(now-timedelta(seconds=1)).isoformat())
            if req['action']=='observe': return snap
            if req['action']=='off': return dict(status='UNKNOWN',executor_stopped=True,drain='UNKNOWN',readonly_restored=True)
            pytest.fail('unexpected action')
        install_ssh(monkeypatch,handler)
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        report=json.loads(capsys.readouterr().out.splitlines()[-1])
        assert report['phase']=='verify_live' and report['status']!='PASS'
        assert report['rollback']['drain']=='UNKNOWN' and report['rollback']['readonly_restored'] is True
        assert calls.count('on')==1 and calls.count('off')==1


def test_preparation_is_explicit_and_preserves_secrets(combined_world,monkeypatch):
    target=combined_world.profile
    import hashlib
    from pathlib import Path
    from tests.test_live_control_host import call_host
    path=Path(target['files']['run_env']); path.write_text('LP_V2_CAPITAL_USDC=100\nUNRELATED_SECRET=SECRET_KEEP\n')
    before=path.read_bytes(); signing=Path(target['files']['signing_key']).read_bytes()
    target['trading']['capital_usdc']='80'
    proof=dict(profile_sha256=hashlib.sha256(json.dumps(target,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
               issued_at=combined_world.clock.isoformat(),window_end='2099-01-01T00:00:00+00:00',operation='prepare')
    request=dict(schema_version=1,action='prepare',profile=target,action_id='b'*32,
                 changes={'LP_V2_CAPITAL_USDC':'80','LP_V2_MARKET':target['trading']['market_condition_id'],'LP_V2_CODE_SHA':target['release']['expected_sha']})
    code,_=call_host(request); assert code==2 and path.read_bytes()==before
    request['confirmation']=proof
    code,result=call_host(request)
    assert code==0 and result['status']=='PASS',result
    assert 'LP_V2_CAPITAL_USDC=80\n' in path.read_text() and 'UNRELATED_SECRET=SECRET_KEEP\n' in path.read_text()
    assert path.with_name(path.name+'.bak').read_bytes()==before
    assert Path(target['files']['signing_key']).read_bytes()==signing
    assert not list(Path(target['files']['state_dir']).glob('journal*'))
    assert combined_world.mutations==[] and combined_world.readonly is True
    assert 'SECRET' not in json.dumps(result)


def seed_run(target):
    from pathlib import Path
    from lp_v2.journal import Journal
    state=Path(target['files']['state_dir'])
    stamp=(datetime.now(UTC)-timedelta(seconds=20)).isoformat()
    run=dict(profile=copy.deepcopy(target),run_id='f'*32,started_at=stamp,journal_seq=0)
    for name,value in [('live-control-run.json',run),('process.json',dict(run_id='f'*32,pid=12345,identity='boot:123'))]:
        p=state/name;p.write_text(json.dumps(value));p.chmod(0o600)
    lock=state/'executor.lock';lock.touch(mode=0o600)
    journal=Journal(state/'journal.sqlite');journal.close();(state/'journal.sqlite').chmod(0o600)
    return run


def off_boundary(target,monkeypatch,*,failure=None,merge_evidence=()):
    from pathlib import Path
    from lp_v2.journal import Journal
    from lp_v2.models import OrderStatus
    state=Path(target['files']['state_dir']); calls=[]
    def command(argv,**kw):
        calls.append(argv)
        if argv[:2]==['systemctl','show']:
            return subprocess.CompletedProcess(argv,0,'MainPID=0\nActiveState=inactive','')
        if argv[:3]==['systemctl','stop','lp-v2-live.service']:
            result=dict(run_id='0'*32 if failure=='old' else 'f'*32,exit_code=1 if failure=='nonzero' else 0)
            if failure!='missing':
                p=state/'stop-result.json';p.write_text(json.dumps(result));p.chmod(0o600)
            journal=Journal(state/'journal.sqlite')
            drain=dict(at=datetime.now(UTC),exit_code=result['exit_code'],orders=[],pending=[],positions=[],inventory=[{'quantity':'3','cost_usdc':'1'}],reconciliation='SUCCESS',accounting_as_of=datetime.now(UTC))
            if merge_evidence!='missing':drain['merges']=merge_evidence
            journal.append('stop_result',drain)
            journal.close()
        if argv[0]=='ss':
            return subprocess.CompletedProcess(argv,1 if failure=='readback' else 0,'','')
        return subprocess.CompletedProcess(argv,0,'','')
    def http(transport,req):
        assert req.method=='GET'
        return __import__('httpx').Response(200,request=req,json=dict(mode='read_only',as_of=datetime.now(UTC).isoformat(),
            capital_usdc=target['trading']['capital_usdc'],run=dict(code_sha=target['release']['expected_sha'],wallet_tail=target['identity']['wallet_address'][-4:])))
    monkeypatch.setattr(subprocess,'run',command)
    monkeypatch.setattr(__import__('httpx').HTTPTransport,'handle_request',http)
    return calls


def test_off_requires_matching_drain_evidence(target,monkeypatch):
    from tests.test_live_control_host import call_host
    seed_run(target);calls=off_boundary(target,monkeypatch)
    code,report=call_host(dict(schema_version=1,action='off',profile=target,run_id='f'*32,action_id='c'*32))
    assert code==0, report
    assert report['executor_stopped'] is True and report['drain']=='PASS' and report['readonly_restored'] is True
    assert report['port_released'] is True
    assert ['systemctl','stop','lp-v2-live.service'] in calls
    assert ['systemctl','start','lp-v2.service'] in calls
    assert not any(any(v in argv for v in ('cancel-all','sell','kill','restart')) for argv in calls)
    assert report['inventory'][0]['quantity']=='3'


def test_off_preserves_unknown_after_process_exit(target,monkeypatch):
    from pathlib import Path
    from tests.test_live_control_host import call_host
    for number,failure in enumerate(('old','missing','nonzero','readback'),1):
        seed_run(target)
        Path(target['files']['state_dir'],'stop-result.json').unlink(missing_ok=True)
        calls=off_boundary(target,monkeypatch,failure=failure)
        code,report=call_host(dict(schema_version=1,action='off',profile=target,run_id='f'*32,action_id=str(number)*32))
        assert code==2 and report['status']!='PASS'
        if failure!='readback': assert report['drain']!='PASS'
        assert report['executor_stopped'] is True
        assert report['inventory'][0]['quantity']=='3'
        if failure=='readback': assert ['systemctl','start','lp-v2.service'] not in calls
        else: assert report['readonly_restored'] is True


def test_off_uses_frozen_run_identity_after_profile_edit(config_home,profile,monkeypatch,capsys):
    run_dir=config_home/'runs';run_dir.mkdir(mode=0o700)
    path=run_dir/'tokyo.json';path.write_text(json.dumps(dict(profile=profile,run_id='f'*32,action_id='a'*32,started_at=datetime.now(UTC).isoformat())));path.chmod(0o600)
    changed=copy.deepcopy(profile);changed['revision']=2;changed['trading']['market_condition_id']='0x'+'9'*64;changed['trading']['capital_usdc']='80'
    (config_home/'profiles/tokyo.json').write_text(json.dumps(changed))
    calls=[]
    def handler(req):
        calls.append(req)
        assert req['profile']==profile and req['run_id']=='f'*32
        return dict(status='PASS',executor_stopped=True,drain='PASS',port_released=True,readonly_restored=True,run_id='f'*32,merge_history=dict(status='PASS',reason='KNOWN_MERGES_TERMINAL'))
    install_ssh(monkeypatch,handler)
    assert main(['live','off'])==0
    report=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report['drain']=='PASS' and [r['action'] for r in calls]==['off']
    # Editing the target itself cannot silently stop either old or new target.
    changed['target']['ssh_host']='another';(config_home/'profiles/tokyo.json').write_text(json.dumps(changed))
    assert main(['live','off'])==2
    assert len(calls)==1


def test_concurrent_control_has_one_mutation_owner(target,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from tests.test_live_control_host import call_host
    seed_run(target); calls=off_boundary(target,monkeypatch)
    underlying=subprocess.run; entered=Event(); release=Event()
    def slow(argv,**kw):
        if argv[:3]==['systemctl','stop','lp-v2-live.service']:
            entered.set(); assert release.wait(5),'independent test watchdog'
        return underlying(argv,**kw)
    monkeypatch.setattr(subprocess,'run',slow)
    request=dict(schema_version=1,action='off',profile=target,run_id='f'*32,action_id='c'*32)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(call_host,request)
        assert entered.wait(5),'independent test watchdog'
        try:
            second=pool.submit(call_host,dict(request,action_id='d'*32))
            code,result=second.result(timeout=2)
            assert code==2 and result['status']=='BUSY'
        finally:
            release.set()
        assert first.result(timeout=5)[0]==0
    assert sum(argv[:3]==['systemctl','stop','lp-v2-live.service'] for argv in calls)==1
    code,result=call_host(request)
    assert code==0 and result['drain']=='PASS'
    assert sum(argv[:3]==['systemctl','stop','lp-v2-live.service'] for argv in calls)==1


def test_reports_never_include_secret_sentinels(config_home,profile,monkeypatch,capsys):
    sentinel='SECRET_SENTINEL_773'
    def handler(req):
        if req['action']=='check':
            report=ready_report(profile);report['debug']={'Authorization':sentinel};report['checks']['files']['reason']=sentinel
            return report
        if req['action']=='import':
            item=copy.deepcopy(profile);item['metadata']['sources']['identity.wallet_address']['source']=sentinel
            return dict(profile=item,raw_headers=sentinel)
        return dict(status='UNKNOWN',reason=sentinel,raw_response=sentinel,headers={'RELAYER_API_KEY':sentinel})
    install_ssh(monkeypatch,handler)
    monkeypatch.setattr('builtins.input',lambda _:'save')
    assert main(['live','check'])==0
    assert sentinel not in str(capsys.readouterr())
    assert main(['live','setup','--profile','tokyo','--ssh-host','synthetic-tokyo'])==2
    assert sentinel not in str(capsys.readouterr())
    directory=config_home/'operations';directory.mkdir(mode=0o700)
    path=directory/('a'*32+'.json');path.write_text(json.dumps(dict(action='on',profile=profile)));path.chmod(0o600)
    assert main(['live','on','--action-id','a'*32])==2
    assert sentinel not in str(capsys.readouterr())
    bad=copy.deepcopy(profile);bad['secret']=sentinel
    (config_home/'profiles/tokyo.json').write_text(json.dumps(bad))
    assert main(['live','profile','show','tokyo'])==2
    assert sentinel not in str(capsys.readouterr())


def test_controller_and_real_helper_complete_valid_start_stop(combined_world,monkeypatch,capsys):
    world=combined_world
    monkeypatch.setattr('builtins.input',lambda _: 'on')
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==0
    started=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert started['verified_rounds']==2 and started['runtime_acceptance']['merge']['status'] in ('NOT_RUN','UNKNOWN')
    assert world.mutations==[('stop','lp-v2.service'),('start','lp-v2-live.service')]
    assert main(['live','off'])==0
    stopped=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert stopped['executor_stopped'] is True and stopped['drain']=='PASS' and stopped['readonly_restored'] is True
    assert stopped['runtime_acceptance']['merge']['status'] in ('NOT_RUN','UNKNOWN')
    assert world.mutations==[('stop','lp-v2.service'),('start','lp-v2-live.service'),('stop','lp-v2-live.service'),('start','lp-v2.service')]
    assert all(request.method=='GET' for request in world.http_requests)
    assert not any(request.url.path in ('/submit','/transaction') for request in world.http_requests)


def test_missing_startup_proof_still_blocks_before_mutation(combined_world,monkeypatch,capsys):
    from pathlib import Path
    import importlib.metadata
    world=combined_world
    credentials=Path(world.profile['files']['l2_credentials']);original=credentials.read_bytes()
    env=Path(world.profile['files']['run_env']);original_env=env.read_bytes()
    for scenario,key in [('auth','relayer_auth'),('allowance','allowance'),('identity','private_identity'),
                         ('proxy','proxy_execution'),('market_freshness','public_market'),('account_freshness','relayer_auth')]:
        with monkeypatch.context() as boundary:
            world.http_overrides.clear();world.stale_public=False;world.advance_on_relayer=False
            if scenario=='auth':world.http_overrides['/transactions']=(401,{'error':'synthetic'})
            if scenario=='allowance':world.http_overrides['/balance-allowance']=(200,{'balance':'200000000','allowances':{}})
            if scenario=='identity':
                changed=json.loads(original);changed['wallet_address']='0x'+'9'*40;credentials.write_text(json.dumps(changed))
            if scenario=='proxy':boundary.setattr(importlib.metadata,'version',lambda package:'0.3.0')
            if scenario=='market_freshness':world.stale_public=True
            if scenario=='account_freshness':world.advance_on_relayer=True
            assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
            report=json.loads(capsys.readouterr().out.splitlines()[-1])
            assert report['checks'][key]['status'] in ('UNKNOWN','FAIL'),report
            assert world.mutations==[] and world.readonly is True and env.read_bytes()==original_env
            credentials.write_bytes(original)


def test_off_receipt_rejects_other_run_action_and_profile(combined_world,config_home,monkeypatch,capsys):
    world=combined_world;profile=world.profile
    monkeypatch.setattr('builtins.input',lambda _:'on')
    assert main(['live','on','--action-id','a'*32,'--window-end','2099-01-01T00:00:00+00:00'])==0
    run_a=world.lease.record['run_id'];capsys.readouterr()
    assert main(['live','off','--action-id','1'*32])==0;capsys.readouterr()
    stops=world.mutations.count(('stop','lp-v2-live.service'))
    assert main(['live','off','--action-id','1'*32])==0;capsys.readouterr()
    assert world.mutations.count(('stop','lp-v2-live.service'))==stops
    assert world.requests[-1]['action']=='result'
    assert main(['live','on','--action-id','b'*32,'--window-end','2099-01-01T00:00:00+00:00'])==0
    run_b=world.lease.record['run_id'];assert run_b!=run_a;capsys.readouterr()
    calls=len(world.requests)
    assert main(['live','off','--action-id','1'*32])==2;capsys.readouterr()
    assert len(world.requests)==calls and world.lease.record['run_id']==run_b
    assert main(['live','off','--action-id','b'*32])==2;capsys.readouterr()
    assert len(world.requests)==calls
    local=config_home/'operations'/('1'*32+'.json')
    forged=json.loads(local.read_text());forged['run_id']=run_b
    forged['profile']=copy.deepcopy(profile);forged['profile']['trading']['capital_usdc']='80'
    local.write_text(json.dumps(forged))
    assert main(['live','off','--action-id','1'*32])==2;capsys.readouterr()
    assert len(world.requests)==calls
    forged['profile']=profile;local.write_text(json.dumps(forged))
    assert main(['live','off','--action-id','1'*32])==2;capsys.readouterr()
    assert world.mutations.count(('stop','lp-v2-live.service'))==stops
    for action,extra in [('off',dict(run_id=run_b)),('off',{}),
        ('result',dict(expected_action='off',run_id=run_b)),('result',dict(expected_action='off')),
        ('result',dict(expected_action='on',run_id=run_a)),('result',dict(run_id=run_a))]:
        code,output=world.invoke(dict(schema_version=1,profile=profile,action=action,action_id='1'*32,**extra))
        assert code==2 and json.loads(output)['status']!='PASS'
        assert world.mutations.count(('stop','lp-v2-live.service'))==stops
    assert world.lease.record['run_id']==run_b
    assert main(['live','off'])==0;capsys.readouterr()


def test_on_rejects_fresh_protective_pause(combined_world,monkeypatch,capsys):
    world=combined_world;monkeypatch.setattr('builtins.input',lambda _:'on')
    for scenario in ('protective','missing'):
        world.pause=scenario;before=len(world.mutations)
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        report=json.loads(capsys.readouterr().out.splitlines()[-1])
        assert report['phase']=='verify_live' and report['status']!='PASS'
        assert report['rollback']['drain']=='PASS' and report['rollback']['readonly_restored'] is True
        assert world.mutations[before:]==[('stop','lp-v2.service'),('start','lp-v2-live.service'),('stop','lp-v2-live.service'),('start','lp-v2.service')]
        assert world.lease is None and world.readonly is True
    world.pause=None
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==0
    capsys.readouterr()
    assert main(['live','off'])==0;capsys.readouterr()


def test_check_retains_safe_step_reasons(combined_world,capsys):
    from pathlib import Path
    world=combined_world;world.fee_blocked=True;world.stale_public=True
    env=Path(world.profile['files']['run_env']);env.write_text(env.read_text().replace('CAPITAL_USDC=100','CAPITAL_USDC=80'))
    assert main(['live','check'])==2
    report=json.loads(capsys.readouterr().out)
    assert report['checks']['run_env']['reason']=='ENV_EXPECTATION_DIFFERS'
    public=report['checks']['public_market']['checks']
    assert public['fees']==dict(status='BLOCKED',reason_code='FEE_POLICY_UNPROVEN')
    assert public['books']==dict(status='BLOCKED',reason_code='BOOK_IDENTITY_CONSTRAINTS_OR_FRESHNESS')
    assert world.mutations==[] and world.readonly is True
    sentinel='SECRET_SENTINEL_DIAGNOSTIC'
    def corrupt(data):
        data['checks']['fees']['reason_code']=sentinel
        data['checks']['fees']['debug']={'Authorization':sentinel}
        data['checks'][sentinel]=dict(status='PASS',reason_code=sentinel)
        data['raw_error']=sentinel
        return data
    world.public_report_transform=corrupt
    assert main(['live','check'])==2
    text=capsys.readouterr().out;safe=json.loads(text)
    assert sentinel not in text and world.profile['files']['run_env'] not in text
    assert safe['checks']['public_market']['checks']['fees']['reason_code']=='CHECK_DETAIL_UNAVAILABLE'
    assert safe['checks']['public_market']['checks']['books']['status']=='BLOCKED'
    assert world.mutations==[]


def test_handoff_rechecked_under_lock_before_switch(combined_world,monkeypatch,capsys):
    import os
    from pathlib import Path
    world=combined_world;profile=world.profile
    monkeypatch.setattr('builtins.input',lambda _:'on')
    original_open=os.open;scenario=None;held=[]
    def lock_boundary(path,*args,**kw):
        fd=original_open(path,*args,**kw)
        if str(path)==str(Path(profile['files']['state_dir'])/'control.lock'):
            held.append(scenario)
            if scenario=='legacy':world.legacy_active=True
            if scenario=='alternate':world.extra_processes[45678]=['/alternate/lp-v2','run','--live','--state-dir','/elsewhere']
            if scenario=='inventory':world.conditional['102']='1'
        return fd
    monkeypatch.setattr(os,'open',lock_boundary)
    env=Path(profile['files']['run_env']);original_env=env.read_bytes()
    for preparation in (False,True):
        for scenario in ('legacy','alternate','inventory'):
            world.legacy_active=False;world.extra_processes.clear();world.conditional={'101':'0','102':'0'}
            before=original_env.replace(b'LP_V2_CAPITAL_USDC=100',b'LP_V2_CAPITAL_USDC=99') if preparation else original_env
            env.write_bytes(before)
            options=['--prepare-run-env'] if preparation else []
            assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00',*options])==2
            report=json.loads(capsys.readouterr().out.splitlines()[-1])
            assert report['status']!='PASS' and held[-1]==scenario
            assert world.mutations==[] and world.readonly is True and env.read_bytes()==before
            assert sum(request['action'] in ('on','prepare') for request in world.requests)==len(held)


@pytest.mark.parametrize('presentation',('lower','checksum','mixed'))
def test_valid_identity_case_variants_need_no_profile_edits(combined_world,config_home,monkeypatch,capsys,presentation):
    import httpx
    from pathlib import Path
    from lp_v2.journal import Journal
    world=combined_world;profile=world.profile
    checksum=copy.deepcopy(profile['identity'])
    # All representations preserve the same independent 20-byte Ethereum identities.
    assert checksum['signer_address']=='0x4a62316623ad457F02cDC5D997deD67a383EC569'
    assert checksum['wallet_address']=='0xa061c806B6F06b2078DC0158F081E09C73aBEC8A'
    def style(address,kind):
        return address.lower() if kind=='lower' else '0x'+address[2:].swapcase() if kind=='mixed' else address
    for key in ('signer_address','wallet_address'):profile['identity'][key]=style(checksum[key],presentation)
    credentials=Path(profile['files']['l2_credentials']);l2=json.loads(credentials.read_text())
    for key in ('signer_address','wallet_address'):l2[key]=style(checksum[key],'mixed' if presentation!='mixed' else 'lower')
    credentials.write_text(json.dumps(l2));credential_bytes=credentials.read_bytes()
    saved=config_home/'profiles/tokyo.json';saved.write_text(json.dumps(profile))
    protected={p:p.read_bytes() for p in (saved,config_home/'config.json',credentials)}
    state=Path(profile['files']['state_dir']);started=world.clock-timedelta(seconds=30);at=world.clock-timedelta(seconds=10)
    prior=copy.deepcopy(profile)
    for key in ('signer_address','wallet_address'):prior['identity'][key]=style(checksum[key],'checksum' if presentation!='checksum' else 'lower')
    record=dict(profile=prior,run_id='f'*32,started_at=started.isoformat(),journal_seq=1)
    for name,payload in [('live-control-run.json',record),('process.json',dict(run_id='f'*32,pid=45678,identity='synthetic-boot:123')),('stop-result.json',dict(run_id='f'*32,exit_code=0))]:
        path=state/name;path.write_text(json.dumps(payload));path.chmod(0o600)
    journal=Journal(state/'journal.sqlite')
    journal.append('identity',dict(owner='lp-v2:'+checksum['wallet_address'].lower(),condition_id=profile['trading']['market_condition_id'],token_ids=('101','102')))
    journal.append('occupation',dict(at=started))
    journal.append('stop_result',dict(at=at,exit_code=0,orders=[],pending=[],positions=[],merges=[],inventory=[],reconciliation='SUCCESS',accounting_as_of=at))
    journal.close();(state/'journal.sqlite').chmod(0o600)
    monkeypatch.setattr('builtins.input',lambda _:'on')
    # Runtime status uses the actual credential's presentation, independent of profile.
    dispatch=httpx.HTTPTransport.handle_request
    def actual_tail(transport,request):
        response=dispatch(transport,request)
        if request.url.path=='/summary':
            data=response.json();data['run']['wallet_tail']=l2['wallet_address'][-4:]
            return httpx.Response(200,json=data,request=request)
        return response
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',actual_tail)
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==0
    report=json.loads(capsys.readouterr().out.splitlines()[-1]);assert report['verified_rounds']==2
    assert main(['live','off'])==0
    report=json.loads(capsys.readouterr().out.splitlines()[-1]);assert report['drain']=='PASS' and report['readonly_restored'] is True
    assert all(path.read_bytes()==value for path,value in protected.items())
    count=len(world.mutations)
    # Semantic business equality never authorizes a changed operation profile.
    altered=copy.deepcopy(profile)
    for key in ('signer_address','wallet_address'):altered['identity'][key]='0x'+altered['identity'][key][2:].swapcase()
    off_request=next(request for request in reversed(world.requests) if request['action']=='off')
    code,raw=world.invoke(dict(schema_version=1,action='result',profile=altered,expected_action='off',
        action_id=off_request['action_id'],run_id=off_request['run_id']))
    assert code==2 and json.loads(raw)['reason']=='ACTION_IDENTITY_MISMATCH'
    on_request=next(request for request in world.requests if request['action']=='on')
    code,raw=world.invoke(dict(on_request,profile=altered))
    assert code==2 and json.loads(raw)['reason']=='CONFIRMATION_REQUIRED'
    assert len(world.mutations)==count
    for field,value in [('wallet_address','0x'+'9'*36+checksum['wallet_address'][-4:]),
                        ('signer_address','0x'+'9'*36+checksum['signer_address'][-4:]),
                        ('signature_type',2),('signature_type',True),('wallet_address','0x1234')]:
        bad=json.loads(credential_bytes);bad[field]=value;credentials.write_text(json.dumps(bad))
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        capsys.readouterr();assert len(world.mutations)==count and world.readonly is True
    credentials.write_bytes(credential_bytes)
    assert all(path.read_bytes()==value for path,value in protected.items())


def test_live_check_preserves_safe_opening_details(combined_world,capsys):
    from tests.test_readonly_sdk import taker_fee_wire
    world=combined_world
    wire=taker_fee_wire();wire['liquidityNum']='825.3674'
    world.http_overrides['/markets/keyset']=(200,{'markets':[wire],'next_cursor':None})
    def check():
        assert main(['live','check'])==2
        return json.loads(capsys.readouterr().out)
    report=check();public=report['checks']['public_market'];row=public['checks']['opening_eligibility']
    assert report['status']=='FAIL' and row['status']=='BLOCKED'
    expected=dict(liquidity_num='825.3674',minimum_liquidity='1000',midpoint='0.47',minimum_midpoint='0.10',maximum_midpoint='0.90',
                  as_of=world.clock.isoformat(),blockers=['LIQUIDITY_BELOW_MIN'])
    assert row['details']==expected
    for corruption in ('extra_only','extra','malformed','bool','nan','stale','foreign'):
        def corrupt(raw):
            detail=raw['checks']['opening_eligibility']['details']
            if corruption=='extra_only':detail['secret']='SECRET_SENTINEL'
            if corruption=='extra':detail['secret']='SECRET_SENTINEL';detail['blockers'].append('SECRET_SENTINEL')
            if corruption=='malformed':detail['liquidity_num']={'secret':'SECRET_SENTINEL'}
            if corruption=='bool':detail['liquidity_num']=True
            if corruption=='nan':detail['liquidity_num']='NaN'
            if corruption=='stale':raw['as_of']='2000-01-01T00:00:00+00:00'
            if corruption=='foreign':raw['market']='0x'+'2'*64
            return raw
        world.public_report_transform=corrupt
        result=check();assert 'SECRET_SENTINEL' not in json.dumps(result)
        safe=result['checks']['public_market']['checks']['opening_eligibility']
        if corruption=='extra_only':assert safe['details']==expected
        else:assert 'details' not in safe
        assert result['status']!='PASS'
    world.public_report_transform=None;wire['feeSchedule']['takerOnly']=False
    row=check()['checks']['public_market']['checks']
    assert row['fees']['status']=='BLOCKED' and row['opening_eligibility']['details']==expected
    assert world.mutations==[] and world.readonly
