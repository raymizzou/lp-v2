"""Target helper uses real parsing/files and fake external HTTP/process boundaries."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess

import httpx
import pytest
from tests.test_live_control_profiles import profile, config_home


@pytest.fixture
def target(tmp_path,profile):
    from eth_account import Account
    from polymarket.environments import PRODUCTION
    from polymarket._internal.wallet import derive_proxy_wallet_address
    p=copy.deepcopy(profile); base=tmp_path/'target'; base.mkdir(mode=0o700)
    p['release']['root']=str(base)
    release=base/'releases'/p['release']['expected_sha']; release.mkdir(parents=True)
    (release/'COMPLETE').write_text(p['release']['expected_sha'])
    (base/'current').symlink_to(release,target_is_directory=True)
    signer=Account.from_key(bytes([7])*32)
    p['identity']['signer_address']=signer.address
    p['identity']['wallet_address']=derive_proxy_wallet_address(signer.address,PRODUCTION.wallet_derivation)
    for name in p['files']:
        p['files'][name]=str(base/name)
    Path(p['files']['state_dir']).mkdir(mode=0o700)
    payloads={'signing_key':'0x'+signer.key.hex(),
        'l2_credentials':json.dumps(dict(key='SECRET_KEY',secret='c2VjcmV0',passphrase='SECRET_PHRASE',**p['identity'])),
        'relayer_credentials':json.dumps(dict(key='SECRET_RELAYER',address=signer.address)),
        'run_env':'LP_V2_MARKET='+p['trading']['market_condition_id']+'\nLP_V2_CAPITAL_USDC=100\nLP_V2_CODE_SHA='+p['release']['expected_sha']+'\n'}
    for name,value in payloads.items():
        path=Path(p['files'][name]); path.write_text(value); path.chmod(0o600)
    return p


@pytest.fixture(autouse=True)
def operator_os(monkeypatch):
    from types import SimpleNamespace
    from threading import local
    from lp_v2.live_control import privileges
    uid,gid=os.getuid(),os.getgid();state=local()
    monkeypatch.setattr(privileges,'geteuid',lambda:getattr(state,'ids',(0,0,0))[1])
    monkeypatch.setattr(privileges,'getresuid',lambda:getattr(state,'ids',(0,0,0)))
    monkeypatch.setattr(privileges,'getresgid',lambda:(0,0,0))
    monkeypatch.setattr(privileges,'getgroups',lambda:[])
    monkeypatch.setattr(privileges,'setgroups',lambda groups:None)
    monkeypatch.setattr(privileges,'setresgid',lambda *ids:None)
    monkeypatch.setattr(privileges,'setresuid',lambda *ids:setattr(state,'ids',ids))
    monkeypatch.setattr(privileges,'seteuid',lambda value:setattr(state,'ids',(getattr(state,'ids',(0,0,0))[0],value,0)))
    monkeypatch.setattr(privileges,'service_account',lambda:SimpleNamespace(pw_uid=uid,pw_gid=gid))


def call_host(payload):
    from lp_v2.live_control.host import main
    output=io.StringIO()
    code=main(stdin=io.StringIO(json.dumps(payload)),stdout=output)
    return code,json.loads(output.getvalue())


def test_check_has_no_remote_mutation_or_signing(target,monkeypatch):
    calls=[]; gets=[]
    def boundary(argv,**kw):
        calls.append(argv)
        assert argv[0] in ('systemctl','git') or 'preflight' in argv
        assert not any(v in argv for v in ('start','stop','restart','enable','daemon-reload'))
        return subprocess.CompletedProcess(argv,2,'{}','')
    def http(transport,request):
        gets.append(request.method)
        assert request.method=='GET'
        return httpx.Response(503,request=request,json={'error':'SECRET_ERROR'})
    monkeypatch.setattr(subprocess,'run',boundary)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',http)
    from polymarket import SecureClient
    monkeypatch.setattr(SecureClient,'create',lambda **kw: pytest.fail('no signing client'))
    base=Path(target['release']['root'])
    before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in base.rglob('*') if p.is_file()}
    for damaged in (False,True):
        if damaged:
            Path(target['files']['l2_credentials']).chmod(0o644)
        code,result=call_host(dict(schema_version=1,action='check',profile=target))
        assert code==2 and result['status']!='PASS'
        assert 'files' in result['checks'] and 'public_market' in result['checks']
        assert 'SECRET' not in json.dumps(result)
    after={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in before}
    assert after==before
    assert all(method=='GET' for method in gets)


def test_account_readiness_requires_authoritative_evidence(target,monkeypatch):
    signer=target['identity']['signer_address']; wallet=target['identity']['wallet_address']
    scenario={}; requests=[]
    def http(transport,request):
        requests.append(request)
        path=request.url.path
        assert request.method=='GET'
        data={'/balance-allowance':{'balance':'200000000','allowances':{
            '0xE111180000d2663C0091e4f400237545B87B996B':'200000000',
            '0xe2222d279d744050d28e00520010520000310F59':'200000000'}},
            '/auth/ban-status/closed-only':{'closed_only':False},
            '/data/orders':{'data':[],'next_cursor':'LTE='},
            '/api/geoblock':{'blocked':False,'country':'US'},'/transactions':[]}
        payload=scenario.get(path,data.get(path,{}))
        return httpx.Response(scenario.get('status:'+path,200),request=request,json=payload)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',http)
    monkeypatch.setattr(subprocess,'run',lambda argv,**kw: subprocess.CompletedProcess(argv,2,'{}',''))
    def checks():
        return call_host(dict(schema_version=1,action='check',profile=target))[1]['checks']
    good=checks()
    for key in ('balance','allowance','closed_only','relayer_auth','private_identity'):
        assert good[key]['status']=='PASS'
    assert good['relayer_merge']['status']=='UNKNOWN'
    relayer=[r for r in requests if r.url.path=='/transactions']
    assert relayer and relayer[0].headers['RELAYER_API_KEY']=='SECRET_RELAYER'
    assert relayer[0].headers['RELAYER_API_KEY_ADDRESS']==signer
    for path,data,key in [('/balance-allowance',{},'balance'),('/balance-allowance',{'balance':True,'allowances':{}},'balance'),
        ('/balance-allowance',{'balance':'200000000','allowances':{}},'allowance'),
        ('/auth/ban-status/closed-only',{},'closed_only'),('/auth/ban-status/closed-only',{'closed_only':'false'},'closed_only'),
        ('/transactions',{'deployed':True},'relayer_auth'),('/transactions',[{'owner':'0x'+'9'*40}],'relayer_auth')]:
        scenario.clear(); scenario[path]=data
        assert checks()[key]['status'] in ('UNKNOWN','FAIL')
    for status in (401,403,500):
        scenario.clear(); scenario['status:/transactions']=status
        assert checks()['relayer_auth']['status']==('FAIL' if status in (401,403) else 'UNKNOWN')
    scenario.clear()
    credentials=Path(target['files']['l2_credentials']); content=json.loads(credentials.read_text()); content['wallet_address']='0x'+'9'*40
    credentials.write_text(json.dumps(content))
    assert checks()['private_identity']['status']=='FAIL'
    assert 'SECRET' not in json.dumps(checks())
    assert not any(r.url.path=='/deployed' for r in requests)


@pytest.fixture
def combined_world(target, config_home, monkeypatch):
    """Real dispatcher/SDK/locks, replacing only host OS/SSH/systemd/HTTP facts."""
    from datetime import datetime, UTC, timedelta
    import contextlib
    import importlib.util
    import stat
    import threading
    from types import SimpleNamespace
    from tests.test_live_control_profiles import known_ssh
    from tests.test_readonly_sdk import taker_fee_wire
    from lp_v2.cli import main as cli_main
    from lp_v2.live_process import ProcessLock
    from lp_v2.journal import Journal
    from lp_v2 import cli
    import datetime as datetime_module
    root=Path(target['release']['root']);release=(root/'current').resolve()
    source_path='src/lp_v2/cli.py'
    source=subprocess.check_output(['git','show','HEAD:'+source_path],text=True)
    (release/'src/lp_v2').mkdir(parents=True)
    (release/source_path).write_text(source)
    launcher=subprocess.check_output(['git','show','HEAD:deploy/run-live'],text=True)
    (release/'deploy').mkdir();(release/'deploy/run-live').write_text(launcher)
    (config_home/'profiles/tokyo.json').write_text(json.dumps(target))
    world=SimpleNamespace(profile=target,clock=datetime.now(UTC),readonly=True,lease=None,
        mutations=[],requests=[],http_requests=[],authority=True,uid=os.getuid(),gid=os.getgid(),
        process_identity_valid=True,http_overrides={},stale_public=False,context=threading.local(),unit_overrides={},proc_argv=None,running_argv=None,extra_processes={},legacy_active=False,legacy_enabled=False,legacy_queries=[],conditional={'101':'0','102':'0'},conditional_status=200,proc_mounts='proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n',gone_processes=set())
    world.readonly_wrapper=False;world.listener_pid=11223;world.readonly_bad_args=False;world.readonly_bad_uid=False;world.readonly_bad_cgroup=False;world.readonly_race=False;world.readonly_reads=0;world.listener_extra=False;world.readonly_gone=False
    world.context.ids=(0,0,0)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return world.clock.astimezone(tz) if tz else world.clock.replace(tzinfo=None)
    monkeypatch.setattr(cli,'datetime',Clock)
    monkeypatch.setattr(datetime_module,'datetime',Clock)
    import time
    monkeypatch.setattr(time,'sleep',lambda seconds:setattr(world,'clock',world.clock+timedelta(seconds=seconds)))
    if importlib.util.find_spec('lp_v2.live_control.privileges'):
        from lp_v2.live_control import privileges
        monkeypatch.setattr(privileges,'geteuid',lambda:world.context.ids[1] if world.authority else world.uid)
        monkeypatch.setattr(privileges,'getresuid',lambda:world.context.ids)
        monkeypatch.setattr(privileges,'getresgid',lambda:(0,0,0))
        monkeypatch.setattr(privileges,'getgroups',lambda:[])
        monkeypatch.setattr(privileges,'setgroups',lambda groups:None)
        monkeypatch.setattr(privileges,'setresgid',lambda *ids:None)
        monkeypatch.setattr(privileges,'setresuid',lambda *ids:setattr(world.context,'ids',ids))
        monkeypatch.setattr(privileges,'seteuid',lambda uid:setattr(world.context,'ids',(world.context.ids[0],uid,world.context.ids[2])))
        monkeypatch.setattr(privileges,'service_account',lambda:SimpleNamespace(pw_uid=world.uid,pw_gid=world.gid))
    aliases={
        '/etc/lp-v2/signing-key':target['files']['signing_key'],
        '/etc/lp-v2/credentials.json':target['files']['l2_credentials'],
        '/etc/lp-v2/relayer.json':target['files']['relayer_credentials'],
        '/opt/lp-v2/state':target['files']['state_dir'],
    }
    original_open=os.open;original_resolve=Path.resolve
    monkeypatch.setattr(os,'open',lambda path,*args,**kw:original_open(aliases.get(str(path),path),*args,**kw))
    monkeypatch.setattr(Path,'resolve',lambda path,*args,**kw:original_resolve(Path(aliases.get(str(path),path)),*args,**kw))
    original_lstat=Path.lstat
    world.repo_access_times=False;world.repo_access_count=0
    world.repo_owner=0;world.repo_mode=0o755;world.repo_origin='https://github.com/raymizzou/lp-v2.git';world.repo_sha=target['release']['expected_sha'];world.git_calls=[]
    (root/'repo/.git').mkdir(parents=True)
    def immutable_lstat(path,*a,**kw):
        info=original_lstat(path,*a,**kw)
        if path==root or path in root.parents or path==root/'repo' or root/'repo' in path.parents:
            world.repo_access_count+=1
            values=list(info)
            if world.repo_access_times:values[7]=world.repo_access_count
            values[4]=world.repo_owner;values[0]=(info.st_mode & ~0o777)|world.repo_mode
            return os.stat_result(values)
        return info
    monkeypatch.setattr(Path,'lstat',immutable_lstat)
    original_stat=Path.stat;original_read_bytes=Path.read_bytes;original_read_text=Path.read_text;original_iterdir=Path.iterdir
    def proc_entries(path):
        if str(path)=='/proc':return iter([Path('/proc')/str(pid) for pid in [os.getpid(),*world.extra_processes]])
        return original_iterdir(path)
    monkeypatch.setattr(Path,'iterdir',proc_entries)
    def proc_stat(path,*a,**kw):
        if str(path) in {f'/proc/{pid}' for pid in world.gone_processes}:raise FileNotFoundError('synthetic gone process')
        if str(path).startswith('/proc/'):
            if str(path) in ('/proc/11223','/proc/11224') and world.readonly_gone:raise FileNotFoundError()
            return SimpleNamespace(st_uid=world.uid if world.process_identity_valid and not (world.readonly_bad_uid and str(path) in ('/proc/11223','/proc/11224')) else world.uid+1)
        return original_stat(Path(aliases.get(str(path),path)),*a,**kw)
    def readonly_args():
        return [str(release/'.venv/bin/lp-v2'),'run','--capital-usdc','100','--code-sha',target['release']['expected_sha'],'--credentials-file','/etc/lp-v2/credentials.json']
    def proc_bytes(path,*a,**kw):
        if str(path) in ('/proc/11223/cmdline','/proc/11224/cmdline'):
            args=['/usr/bin/flock','-n',str(root/'runtime.lock'),str(root/'current/run-readonly')] if world.readonly_wrapper and str(path)=='/proc/11223/cmdline' else readonly_args()
            if world.readonly_bad_args:args=args+['--live']
            return ('\0'.join(args)+'\0').encode()
        for pid,args in world.extra_processes.items():
            if str(path)==f'/proc/{pid}/cmdline':
                if isinstance(args,Exception):raise args
                return ('\0'.join(args)+'\0').encode()
        if str(path)==f'/proc/{os.getpid()}/cmdline':
            return ('\0'.join(world.proc_argv or world.running_argv or ['lp_v2'])+'\0').encode()
        return original_read_bytes(path,*a,**kw)
    def proc_text(path,*a,**kw):
        if str(path) in ('/proc/11223/stat','/proc/11224/stat'):
            world.readonly_reads+=1
            return '1 (flock) '+' '.join(['S']+['0']*18+[str(world.readonly_reads if world.readonly_race else 321)])
        if str(path) in ('/proc/11223/cgroup','/proc/11224/cgroup'):
            return '0::/system.slice/'+('foreign.service' if world.readonly_bad_cgroup else 'lp-v2.service')+'\n'
        if str(path)=='/proc/11223/task/11223/children':return '11224' if world.readonly_wrapper else ''
        if str(path)=='/proc/mounts':return world.proc_mounts
        if str(path)=='/proc/sys/kernel/random/boot_id':return 'synthetic-boot'
        if str(path)==f'/proc/{os.getpid()}/stat':return '1 (lp-v2) '+' '.join(['S']+['0']*18+['123'])
        return original_read_text(path,*a,**kw)
    monkeypatch.setattr(Path,'stat',proc_stat)
    monkeypatch.setattr(Path,'read_bytes',proc_bytes)
    monkeypatch.setattr(Path,'read_text',proc_text)
    def properties(unit):
        live=unit=='lp-v2-live.service';pid=os.getpid() if world.lease else 0
        values=dict(MainPID=str(pid if live else 11223 if world.readonly else 0),
            ActiveState='active' if (world.lease if live else world.readonly) else 'inactive',User='lp-v2',Group='lp-v2',
            ExecStart=str(root/'current/deploy/run-live'),ExecStop=str(root/'current/.venv/bin/lp-v2')+' stop --state-dir '+target['files']['state_dir'],
            EnvironmentFiles=target['files']['run_env'],Conflicts='lp-v2.service',TimeoutStopUSec='2min 30s',Restart='no',UnitFileState='disabled',
            WorkingDirectory=str(root/'current'),Type='simple',UMask='0077',NoNewPrivileges='yes',KillSignal='15',
            ExecStartPre='',ExecStartPost='',ExecCondition='',ExecStopPost='',ExecReload='',Environment='',PassEnvironment='',
            UnsetEnvironment='',RootDirectory='',RootImage='',DynamicUser='no',PAMName='',ExecSearchPath='',SupplementaryGroups='')
        if not live:
            values['ControlGroup']='/system.slice/lp-v2.service'
            values['ExecStart']=' '.join(['/usr/bin/flock','-n',str(root/'runtime.lock'),str(root/'current/run-readonly')]) if world.readonly_wrapper else ' '.join(readonly_args())
        if live:values.update(world.unit_overrides)
        return {k:v for k,v in values.items() if v is not None}
    def child(request):
        # OS identity facade applies only inside the real short-lived target invocation.
        world.context.ids=(0,0,0)
        output=io.StringIO()
        from lp_v2.live_control.host import main
        code=main(stdin=io.StringIO(json.dumps(request)),stdout=output)
        return code,output.getvalue()
    def command(argv,**kw):
        if known_ssh(argv):return known_ssh(argv)
        if argv[0]=='ssh':
            request=json.loads(kw['input']);world.requests.append(request)
            if not world.authority:
                return subprocess.CompletedProcess(argv,1,'','sudo: denied')
            code,output=child(request)
            return subprocess.CompletedProcess(argv,code,output,'')
        if argv[0]=='git':
            world.git_calls.append(argv)
            if 'safe.directory='+str(root/'repo') not in argv:
                return subprocess.CompletedProcess(argv,128,'','fatal: detected dubious ownership in repository')
            if argv[-2:]==['--get','remote.origin.url']:return subprocess.CompletedProcess(argv,0,world.repo_origin+'\n','')
            if 'rev-parse' in argv:return subprocess.CompletedProcess(argv,0,world.repo_sha+'\n','')
            text=source_path+'\n' if 'ls-tree' in argv else launcher if argv[-1].endswith(':deploy/run-live') else source
            return subprocess.CompletedProcess(argv,0,text,'')
        if 'preflight' in argv:
            output=io.StringIO()
            with contextlib.redirect_stdout(output):code=cli_main(argv[1:])
            value=output.getvalue()
            transform=getattr(world,'public_report_transform',None)
            if transform is not None:value=json.dumps(transform(json.loads(value)))
            return subprocess.CompletedProcess(argv,code,value,'')
        if argv[0]=='busctl':
            from lp_v2.live_control import privileges
            assert privileges.geteuid()==world.uid
            assert argv[:5]==['busctl','--json=short','--system','get-property','org.freedesktop.systemd1']
            assert argv[5] in ('/org/freedesktop/systemd1/unit/lp_2dv2_2dlive_2eservice','/org/freedesktop/systemd1/unit/lp_2dv2_2eservice')
            assert argv[6]=='org.freedesktop.systemd1.Service'
            assert argv[7] in ('ExecStartPre','ExecStartPost','ExecCondition','ExecStopPost','ExecReload')
            return subprocess.CompletedProcess(argv,getattr(world,'bus_exit',0),getattr(world,'bus_data','{"type":"a(sasbttttuii)","data":[]}'),'')
        if argv[0]=='systemctl':
            action,unit=argv[1:3]
            if unit=='open-trader-prediction.service':
                from lp_v2.live_control import privileges
                assert action=='show' and argv[3:]==['--property=ActiveState,UnitFileState']
                assert privileges.geteuid()==world.uid,'legacy observation must stay unprivileged'
                world.legacy_queries.append(tuple(argv))
                return subprocess.CompletedProcess(argv,0,'ActiveState='+('active' if world.legacy_active else 'inactive')+'\nUnitFileState='+('enabled' if world.legacy_enabled else 'disabled'),'')
            assert unit in ('lp-v2.service','lp-v2-live.service')
            if action=='show':return subprocess.CompletedProcess(argv,0,'\n'.join(k+'='+v for k,v in properties(unit).items()),'')
            assert action in ('start','stop')
            if importlib.util.find_spec('lp_v2.live_control.privileges'):
                from lp_v2.live_control import privileges
                assert privileges.geteuid()==0,'systemd must use operator privilege'
            world.mutations.append((action,unit))
            if unit=='lp-v2.service':world.readonly=action=='start'
            elif action=='start':
                assert world.lease is None,'one executor only'
                values=dict(line.split('=',1) for line in Path(target['files']['run_env']).read_text().splitlines() if '=' in line)
                world.running_argv=[str(release/'.venv/bin/python'),str(release/'.venv/bin/lp-v2'),'run','--live',
                    '--capital-usdc',values['LP_V2_CAPITAL_USDC'],'--market',values['LP_V2_MARKET'],'--code-sha',values['LP_V2_CODE_SHA'],
                    '--private-key-file','/etc/lp-v2/signing-key','--credentials-file','/etc/lp-v2/credentials.json',
                    '--relayer-credentials-file','/etc/lp-v2/relayer.json','--state-dir','/opt/lp-v2/state']
                previous_umask=os.umask(0o077)
                try:
                    world.lease=ProcessLock(target['files']['state_dir']).__enter__()
                    journal=Journal(Path(target['files']['state_dir'])/'journal.sqlite')
                    if not journal.events('identity'):
                        journal.append('identity',dict(owner='lp-v2:'+target['identity']['wallet_address'].lower(),condition_id=target['trading']['market_condition_id'],token_ids=('101','102')))
                    journal.append('occupation',dict(at=world.clock));journal.close()
                finally:os.umask(previous_umask)
            elif world.lease is not None:
                journal=Journal(Path(target['files']['state_dir'])/'journal.sqlite')
                journal.append('stop_result',dict(at=world.clock,exit_code=0,orders=[],pending=[],positions=[],merges=[],inventory=[],reconciliation='SUCCESS',accounting_as_of=world.clock));journal.close()
                world.lease.finish(0);world.lease.__exit__(None,None,None);world.lease=None;world.running_argv=None
            return subprocess.CompletedProcess(argv,0,'','')
        if argv[0]=='ss':
            pid=os.getpid() if world.lease else world.listener_pid if world.readonly else None
            return subprocess.CompletedProcess(argv,0,'' if pid is None else f'LISTEN 127.0.0.1:8765 users:(("lp-v2",pid={pid},fd=3))'+('\nLISTEN 127.0.0.1:8765 users:(("foreign",pid=99123,fd=3))' if world.listener_extra else ''),'')
        pytest.fail('unexpected external command: '+repr(argv))
    def http(transport,request):
        world.http_requests.append(request)
        assert request.method=='GET'
        path=request.url.path
        if path in world.http_overrides:
            status,data=world.http_overrides[path];return httpx.Response(status,json=data,request=request)
        if path=='/markets/keyset':
            wire=taker_fee_wire()
            if getattr(world,'fee_blocked',False):wire['feeSchedule']['takerOnly']=False
            data={'markets':[wire],'next_cursor':None}
        elif path=='/book':
            token=request.url.params['token_id'];bid,ask=('0.46','0.48') if token=='101' else ('0.51','0.53')
            data=dict(market=target['trading']['market_condition_id'],asset_id=token,
                timestamp=str(int((world.clock-timedelta(seconds=300 if world.stale_public else 1)).timestamp()*1000)),
                bids=[dict(price=bid,size='100')],asks=[dict(price=ask,size='100')],min_order_size='1',tick_size='0.01',neg_risk=False,hash='fixture')
        elif path.startswith('/markets/'):data={'in_play':False}
        elif path=='/balance-allowance' and request.url.params.get('asset_type')=='CONDITIONAL':
            assert dict(request.url.params)==dict(asset_type='CONDITIONAL',signature_type='1',token_id=request.url.params['token_id'])
            assert request.headers['POLY_ADDRESS'].lower()==target['identity']['signer_address'].lower()
            token=request.url.params['token_id'];assert token in ('101','102')
            value=world.conditional.get(token)
            if token=='102' and getattr(world,'conditional_advance',False):world.clock+=timedelta(seconds=120)
            data={} if value is None else dict(balance=value,allowances={})
            return httpx.Response(world.conditional_status,json=data,request=request)
        elif path=='/balance-allowance':data={'balance':'200000000','allowances':{
            '0xE111180000d2663C0091e4f400237545B87B996B':'200000000','0xe2222d279d744050d28e00520010520000310F59':'200000000'}}
        elif path=='/auth/ban-status/closed-only':data={'closed_only':False}
        elif path=='/data/orders':data={'data':[],'next_cursor':'LTE='}
        elif path=='/api/geoblock':data={'blocked':False,'country':'US'}
        elif path=='/transactions':
            if getattr(world,'advance_on_relayer',False):world.clock+=timedelta(seconds=120)
            data=[]
        elif path=='/summary':
            world.clock+=timedelta(seconds=10)
            data=dict(mode='live' if world.lease else 'read_only',as_of=world.clock.isoformat(),capital_usdc=target['trading']['capital_usdc'],
                run=dict(code_sha=target['release']['expected_sha'],wallet_tail=target['identity']['wallet_address'][-4:]),
                accounting=dict(status='SUCCESS',as_of=world.clock.isoformat()),freshness=dict(decision_as_of=world.clock.isoformat()),
                markets=[dict(condition_id=target['trading']['market_condition_id'],eligible=True,skip_reason=None)])
            if world.lease and getattr(world,'pause',None)=='protective':
                data['markets'][0].update(eligible=False,skip_reason='protective pause')
            if world.lease and getattr(world,'pause',None)=='missing':
                del data['markets'][0]['eligible']
        else:pytest.fail('unexpected HTTP path '+path)
        return httpx.Response(200,json=data,request=request)
    monkeypatch.setattr(subprocess,'run',command)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',http)
    world.invoke=child
    yield world
    if world.lease is not None:world.lease.__exit__(None,None,None)


def test_missing_operator_authority_preserves_readonly(combined_world,monkeypatch,capsys):
    import hashlib
    from datetime import datetime,UTC
    from lp_v2.cli import main
    world=combined_world;world.authority=False
    path=Path(world.profile['files']['run_env']);before=path.read_bytes()
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
    capsys.readouterr()
    payload=dict(schema_version=1,action='prepare',profile=world.profile,action_id='b'*32,
        changes=dict(LP_V2_MARKET=world.profile['trading']['market_condition_id'],LP_V2_CAPITAL_USDC='100',LP_V2_CODE_SHA=world.profile['release']['expected_sha']),
        confirmation=dict(operation='prepare',profile_sha256=hashlib.sha256(json.dumps(world.profile,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            issued_at=world.clock.isoformat(),window_end='2099-01-01T00:00:00+00:00'))
    code,output=world.invoke(payload)
    assert code==2 and json.loads(output)['status']!='PASS'
    assert world.readonly is True and world.mutations==[] and path.read_bytes()==before
    assert not (Path(world.profile['files']['state_dir'])/'control-actions').exists()


def test_operator_route_preserves_service_owned_state(combined_world,monkeypatch,capsys):
    from lp_v2.cli import main
    world=combined_world;profile=world.profile
    monkeypatch.setattr('builtins.input',lambda _: 'on')
    credentials={Path(profile['files'][key]):Path(profile['files'][key]).read_bytes() for key in ('signing_key','l2_credentials','relayer_credentials')}
    # A release owned by the application user is never a root execution target.
    from lp_v2.live_control.privileges import BOOTSTRAP
    import sys
    executions=[]
    with monkeypatch.context() as boundary:
        boundary.setattr(os,'geteuid',lambda:0)
        boundary.setattr(os,'execv',lambda *args:executions.append(args))
        boundary.setattr(sys,'argv',['bootstrap',profile['release']['root'],profile['release']['expected_sha']])
        with pytest.raises(PermissionError):exec(BOOTSTRAP,{})
    assert executions==[]
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==0
    capsys.readouterr()
    state=Path(profile['files']['state_dir']);journal=(state/'journal.sqlite').read_bytes()
    for path in state.rglob('*'):
        info=path.stat()
        assert info.st_uid==world.uid
        assert info.st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    count=len(world.mutations)
    for change in ('action','unit','path'):
        request=dict(schema_version=1,action='off',profile=copy.deepcopy(profile),run_id=world.lease.record['run_id'],action_id='d'*32)
        if change=='action':request['action']='restart'
        if change=='unit':request['profile']['services']['live_unit']='other.service'
        if change=='path':request['profile']['files']['state_dir']=str(state.parent/'foreign-state')
        code,_=world.invoke(request)
        assert code==2 and len(world.mutations)==count
    world.process_identity_valid=False
    code,output=world.invoke(dict(schema_version=1,action='off',profile=profile,run_id=world.lease.record['run_id'],action_id='e'*32))
    assert code==2 and len(world.mutations)==count,'foreign process must not be stopped'
    assert (state/'journal.sqlite').read_bytes()==journal
    assert all(path.read_bytes()==content for path,content in credentials.items())
    world.process_identity_valid=True
    assert main(['live','off'])==0
    capsys.readouterr()


def test_off_unknown_merge_remains_unknown(target,monkeypatch):
    from decimal import Decimal as D
    from datetime import datetime,UTC
    from lp_v2.models import MergeResult,MergeRequest,Outcome,Position
    from tests.test_live_control import seed_run,off_boundary
    owner='lp-v2:'+target['identity']['wallet_address'].lower();market=target['trading']['market_condition_id'];at=datetime.now(UTC)
    request=MergeRequest('synthetic-merge',owner,market,'101','102',D('1'),at)
    unknown=MergeResult(request,Outcome.UNKNOWN,D('0'),at)
    terminal=MergeResult(request,Outcome.SUCCESS,D('1'),at,tuple(Position(owner,market,token,D('1'),D('.4'),at) for token in ('101','102')))
    ack=MergeResult(request,Outcome.SUCCESS,D('0'),at)
    for number,(merges,expected) in enumerate([([unknown],'UNKNOWN'),('missing','UNKNOWN'),({},'UNKNOWN'),([ack],'UNKNOWN'),([], 'PASS'),([terminal],'PASS')],1):
        seed_run(target);calls=off_boundary(target,monkeypatch,merge_evidence=merges)
        code,report=call_host(dict(schema_version=1,action='off',profile=target,run_id='f'*32,action_id=str(number)*32))
        assert report['merge_history']['status']==expected
        assert report['executor_stopped'] is True and report['readonly_restored'] is True
        assert report['inventory'][0]['quantity']=='3'
        if expected=='UNKNOWN':
            assert code==2 and report['drain']=='UNKNOWN' and report['status']!='PASS'
            assert report['merge_history']['reason'] in ('MERGE_UNSETTLED','MERGE_HISTORY_MISSING','MERGE_HISTORY_INVALID')
        else:assert code==0 and report['drain']=='PASS'
        assert not any(any(arg in argv for arg in ('merge','submit','cancel')) for argv in calls)


def test_on_binds_installed_launcher_to_checked_files(combined_world,config_home,monkeypatch,capsys):
    from lp_v2.cli import main
    world=combined_world;original=copy.deepcopy(world.profile)
    config=config_home/'profiles/tokyo.json';launcher=Path(original['release']['root'])/'current/deploy/run-live';original_launcher=launcher.read_bytes()
    alternative=Path(original['release']['root'])/'alternate-l2.json';alternative.write_bytes(Path(original['files']['l2_credentials']).read_bytes());alternative.chmod(0o600)
    monkeypatch.setattr('builtins.input',lambda _:'on')
    env=Path(original['files']['run_env']);original_env=env.read_bytes()
    for number,scenario in enumerate(('credentials','state','extra_arg','extra_command','stop_arg','stop_ref','env_ref','env_override','env_input','group','launcher_bytes'),1):
        world.profile.clear();world.profile.update(copy.deepcopy(original));world.unit_overrides.clear();launcher.write_bytes(original_launcher);env.write_bytes(original_env)
        if scenario=='credentials':world.profile['files']['l2_credentials']=str(alternative)
        if scenario=='state':
            alternate=Path(original['release']['root'])/'alternate-state';alternate.mkdir(mode=0o700)
            world.profile['files']['state_dir']=str(alternate)
        if scenario=='extra_arg':world.unit_overrides['ExecStart']=str(launcher)+' --unexpected'
        if scenario=='extra_command':world.unit_overrides['ExecStartPre']='/bin/true'
        if scenario=='stop_arg':world.unit_overrides['ExecStop']=str(launcher.parent.parent/'.venv/bin/lp-v2')+' stop --state-dir '+original['files']['state_dir']+' --unexpected'
        if scenario=='stop_ref':world.unit_overrides['ExecStop']=str(launcher.parent.parent/'.venv/bin/lp-v2')+' stop --state-dir /alternate/state'
        if scenario=='env_ref':world.unit_overrides['EnvironmentFiles']='/another/run.env'
        if scenario=='env_override':world.unit_overrides['Environment']='BASH_ENV=/another/script'
        if scenario=='env_input':env.write_bytes(original_env+b'BASH_ENV=/another/script\n')
        if scenario=='group':world.unit_overrides['Group']='other'
        if scenario=='launcher_bytes':launcher.write_bytes(original_launcher+b'\n# unsupported edit\n')
        config.write_text(json.dumps(world.profile));before=env.read_bytes()
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        capsys.readouterr();assert world.mutations==[] and world.readonly is True and env.read_bytes()==before
        # A directly confirmed helper preparation must bind the target too.
        if scenario=='env_input':continue
        from lp_v2.live_control.profiles import fingerprint
        request=dict(schema_version=1,action='prepare',profile=world.profile,action_id=f'{number:032x}',
            changes=dict(LP_V2_CAPITAL_USDC='100',LP_V2_MARKET=original['trading']['market_condition_id'],LP_V2_CODE_SHA=original['release']['expected_sha']),
            confirmation=dict(operation='prepare',profile_sha256=fingerprint(world.profile),issued_at=world.clock.isoformat(),window_end='2099-01-01T00:00:00+00:00'))
        code,raw=world.invoke(request)
        assert code==2 and env.read_bytes()==before and not env.with_name(env.name+'.bak').exists()
        assert world.mutations==[] and world.readonly is True
    env.write_bytes(original_env)
    world.profile.clear();world.profile.update(original);config.write_text(json.dumps(original));world.unit_overrides.clear();launcher.write_bytes(original_launcher)
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==0
    capsys.readouterr();assert main(['live','off'])==0;capsys.readouterr()


def test_observe_derives_full_identity_from_actual_invocation(combined_world,monkeypatch,capsys):
    from lp_v2.cli import main
    world=combined_world;profile=world.profile
    monkeypatch.setattr('builtins.input',lambda _:'on')
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==0
    capsys.readouterr();run_id=world.lease.record['run_id']
    request=dict(schema_version=1,action='observe',profile=profile,run_id=run_id)
    code,raw=world.invoke(request);good=json.loads(raw)
    assert good['identity']==profile['identity'] and good['executor_running'] is True
    # Same public wallet tail cannot establish full account identity.
    credentials=Path(profile['files']['l2_credentials']);original=credentials.read_bytes()
    changed=json.loads(original);changed['wallet_address']='0x'+'9'*36+profile['identity']['wallet_address'][-4:]
    credentials.write_text(json.dumps(changed))
    code,raw=world.invoke(request);bad=json.loads(raw)
    assert bad.get('identity')!=profile['identity'] and bad.get('executor_running') is not True
    credentials.write_bytes(original)
    # Restoring bytes does not restore continuity across a changed file.
    code,raw=world.invoke(request);assert json.loads(raw).get('executor_running') is not True
    world.proc_argv=list(world.running_argv);world.proc_argv[world.proc_argv.index('--state-dir')+1]='/alternate/state'
    code,raw=world.invoke(request);assert json.loads(raw).get('executor_running') is not True
    world.proc_argv=None;world.process_identity_valid=False
    code,raw=world.invoke(request);assert json.loads(raw).get('executor_running') is not True
    assert 'SECRET' not in raw
    world.process_identity_valid=True
    assert main(['live','off'])==0;capsys.readouterr()


def test_check_refuses_active_legacy_or_alternate_executor(combined_world,monkeypatch,capsys):
    from lp_v2.cli import main
    world=combined_world
    monkeypatch.setattr('builtins.input',lambda _:'on')
    for scenario in ('legacy_active','legacy_enabled','alternate','incomplete','readonly_missing','hidden'):
        world.legacy_active=scenario=='legacy_active';world.legacy_enabled=scenario=='legacy_enabled'
        world.readonly=scenario!='readonly_missing';world.extra_processes.clear()
        world.proc_mounts='proc /proc proc rw,hidepid=2 0 0\n' if scenario=='hidden' else 'proc /proc proc rw 0 0\n'
        if scenario=='alternate':world.extra_processes[45678]=['/another/.venv/bin/lp-v2','run','--live','--state-dir','/alternate/state']
        if scenario=='incomplete':world.extra_processes[45678]=PermissionError('synthetic proc denied')
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        report=json.loads(capsys.readouterr().out.splitlines()[-1])
        assert any(report['checks'][key]['status']!='PASS' for key in ('legacy_service','single_executor'))
        assert world.mutations==[]
    world.legacy_active=world.legacy_enabled=False;world.readonly=True;world.extra_processes.clear();world.proc_mounts='proc /proc proc rw 0 0\n'
    world.extra_processes[45678]=FileNotFoundError('synthetic disappeared');world.gone_processes.add(45678)
    assert main(['live','check'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['checks']['legacy_service']['status']==report['checks']['single_executor']['status']=='PASS'
    assert world.legacy_queries and world.mutations==[]


def test_check_requires_conditional_inventory_handoff(combined_world,monkeypatch,capsys):
    from datetime import timedelta
    from decimal import Decimal as D
    from dataclasses import replace
    from lp_v2.cli import main
    from lp_v2.journal import Journal
    from lp_v2.models import Position,MergeRequest,MergeResult,Outcome
    world=combined_world;profile=world.profile;state=Path(profile['files']['state_dir'])
    monkeypatch.setattr('builtins.input',lambda _:'on')
    before={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    world.conditional['101']='1250000'
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
    capsys.readouterr();assert world.mutations==[]
    assert {p:p.read_bytes() for p in state.rglob('*') if p.is_file()}==before
    for value,status in [(None,200),('1.2',200),('-1',200),('0',503)]:
        world.conditional['101']=value;world.conditional_status=status
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        capsys.readouterr();assert world.mutations==[]
    world.conditional_status=200;world.conditional={'101':'0','102':'0'};world.conditional_advance=True
    assert main(['live','check'])==2
    assert json.loads(capsys.readouterr().out)['checks']['inventory_handoff']['status']=='UNKNOWN'
    assert world.mutations==[]
    world.conditional_advance=False
    assert main(['live','check'])==0
    assert json.loads(capsys.readouterr().out)['checks']['inventory_handoff']['status']=='PASS'
    # An authoritative prior stopped run, with literal owned quantities and costs.
    started=world.clock-timedelta(seconds=30);at=world.clock-timedelta(seconds=10)
    owner='lp-v2:'+profile['identity']['wallet_address'].lower();market=profile['trading']['market_condition_id']
    positions=[Position(owner,market,'101',D('1.25'),D('.5'),at),Position(owner,market,'102',D('2'),D('1.1'),at)]
    record=dict(profile=copy.deepcopy(profile),run_id='f'*32,started_at=started.isoformat(),journal_seq=1)
    process=dict(run_id='f'*32,pid=45678,identity='synthetic-boot:123')
    final=dict(run_id='f'*32,exit_code=0)
    for name,payload in [('live-control-run.json',record),('process.json',process),('stop-result.json',final)]:
        path=state/name;path.write_text(json.dumps(payload));path.chmod(0o600)
    def history(rows,*,merges=(),tokens=('101','102'),reconciliation='SUCCESS'):
        journal=Journal(state/'journal.sqlite')
        if not journal.events():
            journal.append('identity',dict(owner=owner,condition_id=market,token_ids=tokens))
            journal.append('occupation',dict(at=started))
        journal.append('stop_result',dict(at=at,exit_code=0,orders=[],pending=[],positions=rows,merges=merges,inventory=[],reconciliation=reconciliation,accounting_as_of=at))
        journal.close();(state/'journal.sqlite').chmod(0o600)
    world.conditional={'101':'1250000','102':'2000000'}
    history(positions)
    frozen={p:p.read_bytes() for p in state.rglob('*') if p.is_file()}
    assert main(['live','check'])==0
    assert json.loads(capsys.readouterr().out)['checks']['inventory_handoff']['status']=='PASS'
    assert {p:p.read_bytes() for p in state.rglob('*') if p.is_file()}==frozen
    for rows in ([replace(positions[0],owner='foreign'),positions[1]], [positions[0]],
                 [replace(positions[0],cost_usdc=D('NaN')),positions[1]],
                 [replace(positions[0],token_id='999'),positions[1]], positions):
        history(rows,reconciliation='UNKNOWN' if rows is positions else 'SUCCESS')
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        capsys.readouterr();assert world.mutations==[]
    unknown=MergeResult(MergeRequest('pending',owner,market,'101','102',D('1'),started),Outcome.UNKNOWN,D('0'),at)
    history(positions,merges=[unknown])
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2;capsys.readouterr()
    history(positions);final['run_id']='e'*32;(state/'stop-result.json').write_text(json.dumps(final))
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2;capsys.readouterr()
    reads=[r for r in world.http_requests if r.url.path=='/balance-allowance' and r.url.params.get('asset_type')=='CONDITIONAL']
    assert {r.url.params['token_id'] for r in reads}=={'101','102'}
    assert all(r.method=='GET' and r.url.params['signature_type']=='1' for r in reads)
    assert world.mutations==[]


def test_new_state_rejects_orphaned_journal_history(combined_world,monkeypatch,capsys):
    from lp_v2.cli import main
    import stat
    world=combined_world;profile=world.profile;state=Path(profile['files']['state_dir'])
    env=Path(profile['files']['run_env']);env_before=env.read_bytes()
    monkeypatch.setattr('builtins.input',lambda _:'on')
    def snapshot():
        rows={}
        for path in state.rglob('*'):
            info=path.lstat();value=None
            if stat.S_ISLNK(info.st_mode):value=('link',os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                try:value=('bytes',path.read_bytes())
                except OSError as error:value=('unreadable',type(error).__name__)
            rows[str(path.relative_to(state))]=(info.st_ino,info.st_mode,info.st_size,value)
        return rows
    def refused():
        before=snapshot();count=len(world.mutations)
        assert main(['live','check'])==2
        report=json.loads(capsys.readouterr().out)
        assert report['checks']['inventory_handoff']['status']=='UNKNOWN'
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        capsys.readouterr()
        assert snapshot()==before and len(world.mutations)==count and world.readonly is True
        assert env.read_bytes()==env_before and not env.with_name(env.name+'.bak').exists()
    assert main(['live','check'])==0;capsys.readouterr();assert snapshot()=={}
    for suffix in ('-wal','-shm','-journal'):
        for form in ('empty','malformed','unreadable','dangling'):
            artifact=state/('journal.sqlite'+suffix)
            if form=='dangling':artifact.symlink_to(state/'missing-history')
            else:
                artifact.write_bytes(b'' if form=='empty' else b'interrupted-history-not-a-valid-sqlite-sidecar')
                artifact.chmod(0o000 if form=='unreadable' else 0o600)
            refused()
            assert not (state/'journal.sqlite').exists()
            # Only synthetic fixture setup removes its own artifact; production never does.
            if not artifact.is_symlink():artifact.chmod(0o600)
            artifact.unlink()
    assert main(['live','check'])==0;capsys.readouterr();assert snapshot()=={}
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==0;capsys.readouterr()
    assert main(['live','off'])==0;capsys.readouterr()
    closed=snapshot()
    assert main(['live','check'])==0;capsys.readouterr();assert snapshot()==closed
    for suffix in ('-wal','-journal'):
        artifact=state/('journal.sqlite'+suffix)
        artifact.write_bytes(b'hot-or-unsettled-history');artifact.chmod(0o600)
        refused();artifact.unlink()
    assert main(['live','check'])==0;capsys.readouterr();assert snapshot()==closed


def test_check_reads_pinned_immutable_root_repository(combined_world,capsys):
    from lp_v2.cli import main
    world=combined_world;world.repo_access_times=True
    def check():
        main(['live','check']);return json.loads(capsys.readouterr().out)['checks']
    assert check()['artifacts']['status']=='PASS'
    assert world.git_calls and all('safe.directory='+world.profile['release']['root']+'/repo' in cmd for cmd in world.git_calls)
    for field,bad in [('repo_owner',world.uid),('repo_mode',0o777),('repo_origin','https://github.com/foreign/repo.git'),('repo_sha','f'*40)]:
        old=getattr(world,field);setattr(world,field,bad)
        assert check()['artifacts']['status']!='PASS'
        setattr(world,field,old)
    gitdir=Path(world.profile['release']['root'])/'repo/.git'
    config=gitdir/'config';config.write_text('[include]\npath = /foreign/config\n')
    assert check()['artifacts']['status']!='PASS'
    config.unlink()
    gitdir.rmdir();gitdir.symlink_to(gitdir.parent.parent/'releases',target_is_directory=True)
    assert check()['artifacts']['status']!='PASS'
    assert world.mutations==[] and world.readonly
    assert not any('--global' in cmd or '--system' in cmd or '--add' in cmd for cmd in world.git_calls)


def test_check_proves_empty_systemd_exec_properties(combined_world,capsys):
    from lp_v2.cli import main
    world=combined_world
    world.unit_overrides.update({k:None for k in ('ExecStartPre','ExecStartPost','ExecCondition','ExecStopPost','ExecReload')})
    def check():
        main(['live','check']);return json.loads(capsys.readouterr().out)['checks']['service']['status']
    assert check()=='PASS'
    for raw in ('{}','null','{"type":"s","data":[]}','{"type":"a(sasbttttuii)","data":["override"]}','SECRET'):
        world.bus_data=raw;assert check()!='PASS'
    world.bus_data='{"type":"a(sasbttttuii)","data":[]}'
    world.bus_exit=1;assert check()!='PASS'
    world.bus_exit=0;world.unit_overrides['ExecReload']='/bin/foreign';assert check()!='PASS'
    from lp_v2.live_control.host import system_properties
    with pytest.raises(ValueError):system_properties('foreign.service')
    assert world.mutations==[] and world.readonly


def test_check_accepts_owned_readonly_flock_listener(combined_world,capsys):
    from lp_v2.cli import main
    world=combined_world;world.readonly_wrapper=True;world.listener_pid=11224
    def check():
        main(['live','check']);return json.loads(capsys.readouterr().out)['checks']['port']['status']
    assert check()=='PASS'
    world.readonly_wrapper=False;world.listener_pid=11223
    assert check()=='PASS'
    for wrapper in (False,True):
        world.readonly_wrapper=wrapper;world.listener_pid=11224 if wrapper else 11223
        for field in ('readonly_bad_args','readonly_bad_uid','readonly_bad_cgroup','readonly_race','listener_extra','readonly_gone'):
            setattr(world,field,True);assert check()!='PASS';setattr(world,field,False)
        world.listener_pid=99123;assert check()!='PASS'
    assert world.mutations==[] and world.readonly


def test_on_keeps_public_gate_when_inventory_is_known(combined_world,capsys):
    from lp_v2.cli import main
    world=combined_world
    world.http_overrides['/markets/'+world.profile['trading']['market_condition_id']]=(200,{'in_play':True})
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
    report=json.loads(capsys.readouterr().out)
    assert report['checks']['inventory_handoff']['status']=='PASS'
    assert report['checks']['public_market']['checks']['in_play']['status']=='BLOCKED'
    assert report['status']!='PASS' and world.mutations==[] and world.readonly


@pytest.mark.parametrize('prepare_env',[False,True],ids=['on','prepare-on'])
def test_typed_empty_exec_proof_covers_prepare_and_on(combined_world,monkeypatch,capsys,prepare_env):
    from lp_v2.cli import main
    world=combined_world
    world.unit_overrides.update({key:None for key in ('ExecStartPre','ExecStartPost','ExecCondition','ExecStopPost','ExecReload')})
    monkeypatch.setattr('builtins.input',lambda _: 'on')
    assert main(['live','check'])==0
    capsys.readouterr()
    env=Path(world.profile['files']['run_env'])
    original=env.read_bytes()
    credentials={Path(world.profile['files'][key]):Path(world.profile['files'][key]).read_bytes()
                 for key in ('signing_key','l2_credentials','relayer_credentials')}
    if prepare_env:env.write_bytes(original.replace(b'LP_V2_CAPITAL_USDC=100',b'LP_V2_CAPITAL_USDC=80'))
    before=env.read_bytes()
    args=['live','on','--window-end','2099-01-01T00:00:00+00:00']+(['--prepare-run-env'] if prepare_env else [])
    # Fail only the target consumer's fresh proof after public readiness passed.
    boundary=subprocess.run;scenario={}
    def external(argv,**kw):
        if argv[0]=='busctl' and scenario and world.requests[-1]['action'] in ('prepare','on'):
            from lp_v2.live_control import privileges
            assert privileges.geteuid()==world.uid
            return subprocess.CompletedProcess(argv,scenario['exit'],scenario['data'],'')
        return boundary(argv,**kw)
    monkeypatch.setattr(subprocess,'run',external)
    for code,data in ((0,'{"type":"a(sasbttttuii)","data":["override"]}'),(0,'{"type":"s","data":[]}'),(1,'')):
        scenario.update(exit=code,data=data)
        assert main(args)==2
        refused=json.loads(capsys.readouterr().out.splitlines()[-1])
        assert refused['status']!='PASS' and world.mutations==[] and world.readonly
        assert env.read_bytes()==before
    scenario.clear()
    assert main(args)==0
    result=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result['status']=='PASS' and result['verified_rounds']==2
    run_id=result['run_id']
    assert world.mutations==[('stop','lp-v2.service'),('start','lp-v2-live.service')]
    assert dict(line.split('=',1) for line in env.read_text().splitlines())==dict(
        LP_V2_CAPITAL_USDC='100',LP_V2_MARKET='0x'+'1'*64,LP_V2_CODE_SHA='a'*40)
    if not prepare_env:assert env.read_bytes()==original
    if prepare_env:assert env.with_name(env.name+'.bak').read_bytes()==before
    state=Path(world.profile['files']['state_dir'])
    for path in state.rglob('*'):
        info=path.stat()
        assert info.st_uid==world.uid and info.st_mode & 0o777==(0o700 if path.is_dir() else 0o600)
    # Same frozen run can drain even when opening proof and public gate now fail.
    world.bus_exit=1
    world.http_overrides['/markets/'+world.profile['trading']['market_condition_id']]=(200,{'in_play':True})
    assert main(['live','off'])==0
    stopped=json.loads(capsys.readouterr().out)
    assert stopped['status']=='PASS' and stopped['drain']=='PASS'
    assert stopped['readonly_restored'] is True and world.readonly and world.lease is None
    count=len(world.mutations)
    assert main(['live','off'])==0
    capsys.readouterr()
    assert len(world.mutations)==count and world.mutations.count(('start','lp-v2-live.service'))==1
    assert all(path.read_bytes()==data for path,data in credentials.items())
    assert 'SECRET' not in json.dumps(result)+json.dumps(stopped)
    assert json.loads((state/'live-control-run.json').read_text())['run_id']==run_id


@pytest.mark.parametrize('case',['missing','false','true','null','zero','one','string','array','invalid_json','http_failure','timeout'])
def test_live_on_accepts_absent_in_play(combined_world,monkeypatch,capsys,case):
    from lp_v2.cli import main
    from tests.test_live_cli import in_play_http_response
    from polymarket import SecureClient
    world=combined_world;boundary=httpx.HTTPTransport.handle_request;seen=[]
    def http(transport,request):
        if request.url.path=='/markets/'+world.profile['trading']['market_condition_id']:
            seen.append(request);assert request.method=='GET'
            return in_play_http_response(request,case)
        return boundary(transport,request)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',http)
    monkeypatch.setattr(SecureClient,'create',lambda **kw:pytest.fail('host proof must not create signing client'))
    monkeypatch.setattr('builtins.input',lambda _: 'on')
    credentials={Path(world.profile['files'][key]):Path(world.profile['files'][key]).read_bytes() for key in ('signing_key','l2_credentials','relayer_credentials','run_env')}
    code=main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])
    result=json.loads(capsys.readouterr().out.splitlines()[-1])
    if case in ('missing','false'):
        assert code==0 and result['status']=='PASS' and result['verified_rounds']==2
        assert world.mutations==[('stop','lp-v2.service'),('start','lp-v2-live.service')]
        run_id=result['run_id'];state=Path(world.profile['files']['state_dir'])
        assert json.loads((state/'live-control-run.json').read_text())['run_id']==run_id
        assert main(['live','off'])==0
        stopped=json.loads(capsys.readouterr().out)
        assert stopped['status']=='PASS' and stopped['drain']=='PASS' and stopped['readonly_restored'] is True
        count=len(world.mutations)
        assert main(['live','off'])==0;capsys.readouterr()
        assert len(world.mutations)==count and world.mutations.count(('start','lp-v2-live.service'))==1
    else:
        assert code==2 and result['status']!='PASS' and world.mutations==[]
        assert result['checks']['public_market']['checks']['in_play']['status']!='PASS'
    assert world.readonly and world.lease is None and seen
    assert all(path.read_bytes()==data for path,data in credentials.items())
    assert 'SECRET_SENTINEL' not in json.dumps(result)


@pytest.mark.parametrize('mode',['builder','legacy'])
def test_check_and_start_bind_same_builder_credential(combined_world,monkeypatch,capsys,mode):
    from lp_v2.cli import main
    world=combined_world;profile=world.profile
    path=Path(profile['files']['relayer_credentials'])
    payload=dict(key='SECRET_RELAYER',address=profile['identity']['signer_address'])
    if mode=='builder':payload.update(kind='builder',secret='c3ludGhldGljLXNlY3JldA',passphrase='SECRET_PHRASE')
    path.write_text(json.dumps(payload))
    credentials={Path(profile['files'][k]):(Path(profile['files'][k]).read_bytes(),Path(profile['files'][k]).stat().st_uid) for k in ('signing_key','l2_credentials','relayer_credentials')}
    monkeypatch.setattr('builtins.input',lambda _:'on')
    assert main(['live','check'])==0
    checked=json.loads(capsys.readouterr().out)
    assert checked['checks']['relayer_auth']['status']=='PASS'
    # Malformed explicit schemas/unsafe headers and unprotected files never start.
    bads=[dict(payload,address='0x'+'9'*40),dict(payload,key=False),dict(payload,unexpected='x'),dict(payload,key='SECRET\nHEADER')]
    for bad in bads:
        path.write_text(json.dumps(bad))
        assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
        assert 'SECRET' not in capsys.readouterr().out
        assert world.mutations==[]
    path.write_text(json.dumps(payload));path.chmod(0o644)
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==2
    capsys.readouterr();assert world.mutations==[]
    path.chmod(0o600)
    assert main(['live','on','--window-end','2099-01-01T00:00:00+00:00'])==0
    started=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert started['status']=='PASS' and started['verified_rounds']==2
    run_id=started['run_id']
    assert world.mutations.count(('start','lp-v2-live.service'))==1
    request=dict(schema_version=1,action='observe',profile=profile,run_id=run_id)
    code,raw=world.invoke(request)
    assert json.loads(raw)['executor_running'] is True
    # Full identity and metadata continuity are both binding, including restored bytes.
    l2=Path(profile['files']['l2_credentials']);original=l2.read_bytes()
    changed=json.loads(original);changed['wallet_address']='0x'+'9'*36+profile['identity']['wallet_address'][-4:]
    l2.write_text(json.dumps(changed))
    code,raw=world.invoke(request);assert json.loads(raw).get('executor_running') is not True
    l2.write_bytes(original)
    code,raw=world.invoke(request);assert json.loads(raw).get('executor_running') is not True
    before=path.read_bytes();path.write_bytes(before)
    code,raw=world.invoke(request);assert json.loads(raw).get('executor_running') is not True
    count=len(world.mutations);world.process_identity_valid=False
    code,raw=world.invoke(dict(schema_version=1,action='off',profile=profile,run_id=run_id,action_id='e'*32))
    assert code==2 and len(world.mutations)==count
    world.process_identity_valid=True
    world.http_overrides['/markets/'+profile['trading']['market_condition_id']]=(200,{'in_play':True})
    assert main(['live','off'])==0
    stopped=json.loads(capsys.readouterr().out)
    assert stopped['drain']=='PASS' and stopped['readonly_restored'] is True
    count=len(world.mutations)
    assert main(['live','off'])==0
    capsys.readouterr();assert len(world.mutations)==count
    assert all(p.read_bytes()==data and p.stat().st_uid==uid for p,(data,uid) in credentials.items())
    assert 'SECRET' not in json.dumps(started)+json.dumps(stopped)+raw
    relayer=[r for r in world.http_requests if r.url.path=='/transactions']
    assert relayer and all(r.method=='GET' for r in world.http_requests)
    assert ('POLY_BUILDER_API_KEY' in relayer[0].headers)==(mode=='builder')
    assert not any(r.url.path=='/deployed' for r in world.http_requests)
