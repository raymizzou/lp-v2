"""Short-lived target helper. JSON stdin only; no HTTP mutation surface."""
import json
from pathlib import Path
import subprocess
import sys
from lp_v2.live_control.profiles import validate, read_json, secure_directory, now
from lp_v2.cli import owner_file


def fact(status, reason):
    return dict(status=status,reason=reason,as_of=now())


def run(argv, timeout=15):
    if argv[:1]==['systemctl']:
        from lp_v2.live_control.privileges import systemctl
        return systemctl(argv,timeout=timeout)
    if argv[:1]==['git']:
        return subprocess.run(argv,text=True,capture_output=True,timeout=timeout,check=False,
            env=dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin',LANG='C',GIT_CONFIG_NOSYSTEM='1',
                GIT_CONFIG_GLOBAL='/dev/null',GIT_NO_REPLACE_OBJECTS='1',GIT_OPTIONAL_LOCKS='0'))
    return subprocess.run(argv,text=True,capture_output=True,timeout=timeout,check=False)


def check(profile):
    from lp_v2.live_control.privileges import operator_ready
    checks={'operator_authority':fact('PASS' if operator_ready() else 'UNKNOWN','EXISTING_OPERATOR_SUDO')}
    root=Path(profile['release']['root']); expected=profile['release']['expected_sha']
    try:
        current=(root/'current').resolve(strict=True)
        good=current==root/'releases'/expected and (current/'COMPLETE').read_text().strip()==expected
        checks['release']=fact('PASS' if good else 'FAIL','RELEASE_IDENTITY')
    except Exception:
        checks['release']=fact('UNKNOWN','RELEASE_UNAVAILABLE')
    try:
        for name,path in profile['files'].items():
            if name=='state_dir': secure_directory(Path(path))
            else:
                with owner_file(path): pass
        checks['files']=fact('PASS','OWNED_PRIVATE_FILES')
    except Exception:
        checks['files']=fact('FAIL','FILE_PERMISSIONS')
    try:
        result=run([str(root/'current/.venv/bin/lp-v2'),'preflight','--market',profile['trading']['market_condition_id'],
                    '--capital-usdc',profile['trading']['capital_usdc'],'--code-sha',expected])
        public=json.loads(result.stdout)
    except Exception:
        public={}
    checks.update(filesystem_checks(profile))
    checks['legacy_service']=legacy_service_check()
    checks.update(account_checks(profile))
    checks['public_market']=fact('UNKNOWN','PREFLIGHT_CAPABILITY_UNVERIFIED')
    return dict(schema_version=1,status='UNKNOWN',as_of=now(),checks=checks,public_market=safe_public(profile,public))



def account_checks(profile):
    from lp_v2.readonly_sdk import L2Credentials, ReadOnlySDK
    from datetime import datetime, UTC
    names=('private_identity','balance','allowance','closed_only','legacy_orders','region','relayer_auth','relayer_merge','proxy_execution','inventory_handoff')
    result={name:fact('UNKNOWN','ACCOUNT_EVIDENCE_MISSING') for name in names}
    try:
        credentials=L2Credentials(**read_json(profile['files']['l2_credentials']))
        from eth_account import Account
        from polymarket._internal.wallet import classify_wallet_type
        from polymarket.environments import PRODUCTION
        with owner_file(profile['files']['signing_key']) as source:
            signer=Account.from_key(source.read().strip()).address
        identity=profile['identity']
        valid=(type(credentials.signature_type) is int and credentials.signature_type==1
               and credentials.signer_address.lower()==signer.lower()==identity['signer_address'].lower()
               and credentials.wallet_address.lower()==identity['wallet_address'].lower()
               and classify_wallet_type(signer=signer,wallet=credentials.wallet_address,config=PRODUCTION.wallet_derivation)=='POLY_PROXY')
        result['private_identity']=fact('PASS' if valid else 'FAIL','FULL_SIGNER_PROXY_IDENTITY')
        if not valid:
            return result
        relayer=read_json(profile['files']['relayer_credentials'])
        sdk=ReadOnlySDK(credentials=credentials)
        try:
            result.update(sdk.read_account_readiness(datetime.now(UTC),capital_usdc=profile['trading']['capital_usdc'],identity=identity,relayer=relayer))
            try:
                from lp_v2.readonly_sdk import ConditionalIdentityUnavailable
                from lp_v2.live_control.handoff import inventory_handoff
                from lp_v2.rotation_state import control_market_profile
                active_profile=control_market_profile(profile)
                quantities=sdk.read_conditional_inventory(datetime.now(UTC),active_profile['trading']['market_condition_id'])
                result['inventory_handoff']=inventory_handoff(profile,quantities)
            except ConditionalIdentityUnavailable:
                result['inventory_handoff']=fact('UNKNOWN','CONDITIONAL_IDENTITY_UNAVAILABLE')
            except Exception:
                result['inventory_handoff']=fact('UNKNOWN','CONDITIONAL_BALANCE_UNAVAILABLE')
        finally:
            sdk.close()
    except Exception:
        pass
    return result


def confirmed(request,operation):
    from lp_v2.live_control.profiles import fingerprint,timestamp
    from lp_v2.live_control.workflow import fresh
    from datetime import datetime,UTC
    proof=request.get('confirmation',{})
    return (proof.get('operation')==operation and proof.get('profile_sha256')==fingerprint(request['profile'])
            and fresh(proof.get('issued_at')) and timestamp(proof.get('window_end'))>datetime.now(UTC))


def prepare(request):
    from lp_v2.live_control.profiles import atomic_bytes
    if not confirmed(request,'prepare'):
        return dict(status='UNKNOWN',reason='CONFIRMATION_REQUIRED')
    profile=request['profile']; changes=request.get('changes')
    expected=dict(LP_V2_CAPITAL_USDC=profile['trading']['capital_usdc'],
                  LP_V2_MARKET=profile['trading']['market_condition_id'],LP_V2_CODE_SHA=profile['release']['expected_sha'])
    if changes!=expected:
        return dict(status='UNKNOWN',reason='PREPARATION_INVALID')
    from lp_v2.live_control.invocation import binding
    try:
        binding(profile,system_properties(profile['services']['live_unit'],prove_exec=True),expected_env(profile),run)
    except Exception:
        return dict(status='UNKNOWN',reason='EFFECTIVE_SERVICE_CONTRACT')
    from lp_v2.live_control.workflow import normalize_report
    readiness=normalize_report(profile,check(profile))
    if any(readiness['checks'][key]['status']!='PASS' for key in
           ('private_identity','single_executor','legacy_service','legacy_orders','inventory_handoff')):
        return readiness
    path=Path(profile['files']['run_env'])
    with owner_file(path) as source:
        old=source.read()
    lines=[line for line in old.splitlines() if line.split('=',1)[0] not in expected]
    value=('\n'.join(lines+[key+'='+value for key,value in expected.items()])+'\n').encode()
    atomic_bytes(path.with_name(path.name+'.bak'),old.encode())
    atomic_bytes(path,value)
    with owner_file(path) as source:
        if source.read().encode()!=value:
            return dict(status='UNKNOWN',reason='PREPARATION_READBACK_FAILED')
    return dict(status='PASS',reason='ENV_PREPARED')



def process_stopped(profile):
    import os,fcntl
    try:
        with owner_file(Path(profile['files']['state_dir'])/'executor.lock') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            return True
    except Exception:
        return False


def journal_path(profile):
    from lp_v2.rotation_state import control_market_profile, market_path
    state = Path(profile['files']['state_dir'])
    active = control_market_profile(profile)['trading']['market_condition_id']
    path = market_path(state,active)
    if (state/'rotation.sqlite').exists() or path.exists() or path.is_symlink():
        return path
    return state/'journal.sqlite'


def journal_present(profile):
    """Do not mistake interrupted sidecars for new state or recover them on read."""
    import os
    path=journal_path(profile)
    try:
        path.lstat();present=True
    except FileNotFoundError:
        present=False
    for suffix in ('-wal','-shm','-journal'):
        sidecar=path.with_name(path.name+suffix)
        try:sidecar.lstat()
        except FileNotFoundError:continue
        if not present:raise ValueError('orphan journal history')
        # owner_file refuses symlinks, unreadable/foreign modes and non-regular
        # files without following a sidecar into unrelated data.
        with owner_file(sidecar) as source:
            if suffix in ('-wal','-journal') and os.fstat(source.fileno()).st_size:
                raise ValueError('unsettled journal history')
    return present


def journal_rows(profile,kind=None):
    import sqlite3
    from lp_v2.journal import decode
    path=journal_path(profile)
    # No Journal constructor, pragmas or database creation on observational reads.
    if not journal_present(profile):raise ValueError('journal missing')
    with owner_file(path): pass
    db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)
    try:
        if kind is None:
            return [(seq,event,decode(payload)) for seq,event,payload in db.execute('SELECT seq,kind,payload FROM events ORDER BY seq')]
        return [(seq,decode(payload)) for seq,payload in db.execute('SELECT seq,payload FROM events WHERE kind=? ORDER BY seq',(kind,))]
    finally:
        db.close()


def inventory_report(rows):
    from decimal import Decimal
    result=[]
    for row in rows:
        values={}
        for key in ('quantity','cost_usdc','valuation_usdc'):
            value=row.get(key)
            if value is None: continue
            number=Decimal(str(value))
            if not number.is_finite() or number<0: raise ValueError()
            values[key]=str(number)
        result.append(values)
    return result



def merge_history_report(profile,merges):
    from decimal import Decimal
    from lp_v2.models import MergeResult,MergeRequest,Position,Outcome
    if merges is None:return dict(status='UNKNOWN',reason='MERGE_HISTORY_MISSING')
    if not isinstance(merges,(list,tuple)):return dict(status='UNKNOWN',reason='MERGE_HISTORY_INVALID')
    try:
        seen=set()
        def money(value,positive=False):
            return isinstance(value,Decimal) and value.is_finite() and value>=0 and (not positive or value>0)
        for result in merges:
            if not isinstance(result,MergeResult) or not isinstance(result.request,MergeRequest):raise ValueError()
            request=result.request
            if (not isinstance(request.merge_id,str) or not request.merge_id or request.merge_id in seen
                    or request.owner!='lp-v2:'+profile['identity']['wallet_address'].lower()
                    or request.condition_id!=profile['trading']['market_condition_id']
                    or not isinstance(request.yes_token_id,str) or not isinstance(request.no_token_id,str)
                    or not request.yes_token_id or not request.no_token_id or request.yes_token_id==request.no_token_id
                    or not isinstance(result.outcome,Outcome) or type(request.generation) is not int or request.generation<0
                    or request.created_at.tzinfo is None or result.observed_at.tzinfo is None or result.observed_at<request.created_at
                    or not money(request.quantity,True) or not money(result.received_usdc)):
                raise ValueError()
            seen.add(request.merge_id)
            if result.outcome==Outcome.UNKNOWN:
                return dict(status='UNKNOWN',reason='MERGE_UNSETTLED',count=len(merges))
            if result.outcome==Outcome.FAILED:
                if result.received_usdc!=0 or result.consumed_positions:raise ValueError()
            elif result.outcome==Outcome.SUCCESS:
                legs=result.consumed_positions
                if result.received_usdc!=request.quantity:
                    return dict(status='UNKNOWN',reason='MERGE_UNSETTLED',count=len(merges))
                if (len(legs)!=2 or any(not isinstance(p,Position) for p in legs)
                        or {p.token_id for p in legs}!={request.yes_token_id,request.no_token_id}
                        or any(p.owner!=request.owner or p.condition_id!=request.condition_id or p.quantity!=request.quantity
                               or not money(p.quantity,True) or not money(p.cost_usdc) for p in legs)):
                    raise ValueError()
            else:raise ValueError()
        return dict(status='PASS',reason='KNOWN_MERGES_TERMINAL',count=len(merges))
    except Exception:
        return dict(status='UNKNOWN',reason='MERGE_HISTORY_INVALID')


def off(request):
    from lp_v2.live_control.profiles import timestamp
    from lp_v2.live_control.identity import matching_tail
    profile=request['profile']; state=Path(profile['files']['state_dir'])
    record=read_json(state/'live-control-run.json')
    if record['profile']!=profile or record['run_id']!=request.get('run_id'):
        return dict(status='UNKNOWN',reason='FROZEN_RUN_MISMATCH')
    process=read_json(state/'process.json')
    if process['run_id']!=record['run_id']:
        return dict(status='UNKNOWN',reason='PROCESS_RUN_MISMATCH')
    live=system_properties(profile['services']['live_unit'])
    if live.get('MainPID')!='0':
        from lp_v2.live_process import process_identity
        identity=process_identity(process['pid'])
        if live.get('MainPID')!=str(process['pid']) or identity is None or identity!=process['identity']:
            return dict(status='UNKNOWN',reason='PROCESS_IDENTITY_UNVERIFIED')
    elif not process_stopped(profile):
        return dict(status='UNKNOWN',reason='PROCESS_IDENTITY_UNVERIFIED')
    result=run(['systemctl','stop',profile['services']['live_unit']],timeout=160)
    stopped=process_stopped(profile)
    port=run(['ss','-H','-ltn','sport = :8765'])
    released=port.returncode==0 and port.stdout.strip()==''
    drained='UNKNOWN'; inventory=[];merges=dict(status='UNKNOWN',reason='MERGE_HISTORY_MISSING')
    try:
        seq,drain=journal_rows(profile,'stop_result')[-1]
        inventory=inventory_report(drain['inventory'])
        from lp_v2.rotation_state import control_market_profile,run_journal_sequence
        merges=merge_history_report(control_market_profile(profile),drain.get('merges'))
        final=read_json(state/'stop-result.json')
        from lp_v2.live_process import stop_result_matches
        if (stop_result_matches(process,final) and type(final['exit_code']) is int and final['exit_code']==0
                and type(drain['exit_code']) is int and drain['exit_code']==0 and drain['reconciliation']=='SUCCESS'
                and seq>run_journal_sequence(record,control_market_profile(profile)['trading']['market_condition_id']) and timestamp(drain['at'].isoformat())>timestamp(record['started_at'])
                and merges['status']=='PASS' and not drain['pending'] and all(order.authoritative and order.status.value in ('filled','cancelled') for order in drain['orders'])):
            drained='PASS'
        elif stop_result_matches(process,final) and final['exit_code']!=0:
            drained='FAIL'
    except Exception:
        pass
    restored=False
    if stopped and released:
        restore=run(['systemctl','start',profile['services']['readonly_unit']])
        if restore.returncode==0:
            try:
                import httpx
                from lp_v2.live_control.workflow import fresh
                with httpx.Client(timeout=5,trust_env=False,follow_redirects=False) as client:
                    response=client.get('http://127.0.0.1:8765/summary'); response.raise_for_status(); row=response.json()
                restored=(row['mode']=='read_only' and fresh(row['as_of']) and row['run']['code_sha']==profile['release']['expected_sha']
                          and row['capital_usdc']==profile['trading']['capital_usdc']
                          and matching_tail(row['run']['wallet_tail'],profile['identity']['wallet_address']))
            except Exception:
                pass
    return dict(status='PASS' if result.returncode==0 and stopped and released and drained=='PASS' and restored else 'UNKNOWN',
        executor_stopped=stopped,port_released=released,drain=drained,readonly_restored=restored,inventory=inventory,merge_history=merges,run_id=record['run_id'])



def mutate(request):
    import os,fcntl,re,stat
    from lp_v2.live_control.profiles import atomic_json,fingerprint
    profile=request['profile']; action=request['action']; action_id=request.get('action_id')
    if not isinstance(action_id,str) or not re.fullmatch('[0-9a-f]{32}',action_id):
        return dict(status='UNKNOWN',reason='ACTION_INVALID')
    if action=='off' and (not isinstance(request.get('run_id'),str) or not re.fullmatch('[0-9a-f]{32}',request['run_id'])):
        return dict(status='UNKNOWN',reason='ACTION_IDENTITY_MISMATCH')
    if action in ('on','prepare') and not confirmed(request,action):
        return dict(status='UNKNOWN',reason='CONFIRMATION_REQUIRED')
    from lp_v2.live_control.privileges import operator_ready
    if not operator_ready():
        return dict(status='UNKNOWN',reason='OPERATOR_AUTHORITY_UNAVAILABLE')
    validate_mutation_paths(request)
    state=Path(profile['files']['state_dir']);secure_directory(state)
    fd=os.open(state/'control.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError()
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(status='BUSY',reason='CONTROL_BUSY')
        directory=state/'control-actions';directory.mkdir(mode=0o700,exist_ok=True);secure_directory(directory)
        path=directory/(action_id+'.json')
        if path.exists() or path.is_symlink():
            prior=read_json(path)
            if (prior.get('profile_sha256')!=fingerprint(profile) or prior.get('action')!=action
                    or (action=='off' and prior.get('run_id')!=request['run_id'])):
                return dict(status='UNKNOWN',reason='ACTION_IDENTITY_MISMATCH')
            return dict(prior['result'],action=action,**({'run_id':prior['run_id']} if action=='off' else {}))
        record=dict(action=action,run_id=request.get('run_id') if action=='off' else None,profile_sha256=fingerprint(profile),result=dict(status='UNKNOWN',reason='ACTION_PENDING'),as_of=now())
        atomic_json(path,record)
        try:
            result=off(request) if action=='off' else start(request) if action=='on' else prepare(request)
        except Exception:
            result=dict(status='UNKNOWN',reason='ACTION_OUTCOME_UNKNOWN')
        record['result']=result;atomic_json(path,record)
        return dict(result,action=action,**({'run_id':record['run_id']} if action=='off' else {}))
    finally:
        os.close(fd)


def action_result(request):
    import re
    from lp_v2.live_control.profiles import fingerprint
    action_id=request.get('action_id')
    if not isinstance(action_id,str) or not re.fullmatch('[0-9a-f]{32}',action_id):
        raise ValueError()
    profile=request['profile']
    state=Path(profile['files']['state_dir']);secure_directory(state);secure_directory(state/'control-actions')
    record=read_json(state/'control-actions'/(action_id+'.json'))
    expected=request.get('expected_action')
    if (expected not in ('on','off','prepare') or record.get('action')!=expected
            or record.get('profile_sha256')!=fingerprint(profile)
            or (expected=='off' and (not isinstance(request.get('run_id'),str)
                or not re.fullmatch('[0-9a-f]{32}',request['run_id']) or record.get('run_id')!=request['run_id']))):
        return dict(status='UNKNOWN',reason='ACTION_IDENTITY_MISMATCH')
    return dict(record['result'],action=expected,**({'run_id':record['run_id']} if expected=='off' else {}))



def env_values(profile):
    values={}
    with owner_file(profile['files']['run_env']) as source:
        for line in source:
            line=line.strip()
            if not line or line.startswith('#'): continue
            key,separator,value=line.partition('=')
            if not separator or key in values: raise ValueError()
            values[key]=value
    return values


def expected_env(profile):
    return dict(LP_V2_CAPITAL_USDC=profile['trading']['capital_usdc'],LP_V2_MARKET=profile['trading']['market_condition_id'],LP_V2_CODE_SHA=profile['release']['expected_sha'])


def system_properties(unit,*,prove_exec=False):
    from lp_v2.live_control.privileges import PROPERTIES, empty_exec_property
    if unit not in ('lp-v2.service','lp-v2-live.service'):raise ValueError('foreign unit')
    response=run(['systemctl','show',unit,PROPERTIES])
    if response.returncode!=0: raise ValueError()
    values=dict(line.split('=',1) for line in response.stdout.splitlines() if '=' in line)
    for key in ('ExecStartPre','ExecStartPost','ExecCondition','ExecStopPost','ExecReload'):
        if prove_exec and key not in values:
            empty_exec_property(unit,key)
            values[key]=''
    return values


def filesystem_checks(profile):
    checks={}
    root=Path(profile['release']['root']); current=root/'current'
    try:
        from lp_v2.live_control.source import read
        listing=read(profile,['ls-tree','-r','--name-only',profile['release']['expected_sha'],'src/lp_v2'],run)
        paths=listing.stdout.splitlines()
        if listing.returncode or not paths: raise ValueError()
        import importlib.util
        good=True
        for name in paths:
            if not name.endswith('.py'): continue
            reference=read(profile,['show',profile['release']['expected_sha']+':'+name],run)
            if reference.returncode: raise ValueError()
            module=name[4:-3].replace('/','.')
            if module.endswith('.__init__'): module=module[:-9]
            spec=importlib.util.find_spec(module)
            good=good and (current/name).read_text()==reference.stdout and spec is not None and Path(spec.origin).read_text()==reference.stdout
        checks['artifacts']=fact('PASS' if good else 'FAIL','SOURCE_RUNTIME_IDENTITY')
    except Exception:
        checks['artifacts']=fact('UNKNOWN','ARTIFACT_PROOF_MISSING')
    try:
        actual=env_values(profile); expected=expected_env(profile)
        good=all(actual.get(key)==value for key,value in expected.items())
        checks['run_env']=fact('PASS' if good else 'FAIL','ENV_EXPECTATION_DIFFERS')
    except Exception:
        checks['run_env']=fact('UNKNOWN','ENV_UNAVAILABLE')
    state=Path(profile['files']['state_dir'])
    try:
        secure_directory(state)
        if journal_present(profile):
            identities=journal_rows(profile,'identity')
            from lp_v2.rotation_state import control_market_profile
            active_profile=control_market_profile(profile)
            good=bool(identities) and all(row['owner']=='lp-v2:'+profile['identity']['wallet_address'].lower()
                and row['condition_id']==active_profile['trading']['market_condition_id'] for _,row in identities)
            checks['state_identity']=fact('PASS' if good else 'FAIL','JOURNAL_FROZEN_IDENTITY')
        else:
            checks['state_identity']=fact('PASS','EXPLICIT_NEW_STATE')
    except Exception:
        checks['state_identity']=fact('UNKNOWN','STATE_IDENTITY_UNAVAILABLE')
    try:
        live=system_properties(profile['services']['live_unit'],prove_exec=True)
        readonly=system_properties(profile['services']['readonly_unit'])
        valid=(live.get('User')=='lp-v2' and str(current/'deploy/run-live') in live.get('ExecStart','')
            and str(current/'.venv/bin/lp-v2') in live.get('ExecStop','') and ' stop ' in live.get('ExecStop','')
            and profile['files']['state_dir'] in live.get('ExecStop','')
            and profile['files']['run_env'] in live.get('EnvironmentFiles','')
            and 'lp-v2.service' in live.get('Conflicts','')
            and live.get('Restart')=='no' and live.get('UnitFileState') in ('disabled','static')
            and readonly.get('User')=='lp-v2')
        from lp_v2.live_control.invocation import binding
        try:
            binding(profile,live,env_values(profile),run)
        except Exception:
            valid=False
        checks['service']=fact('PASS' if valid else 'FAIL','EFFECTIVE_SERVICE_CONTRACT')
        stopped=live.get('MainPID')=='0' and live.get('ActiveState') in ('inactive','failed')
        lock_free=process_stopped(profile) if (state/'executor.lock').exists() else True
        scan=local_executor_check()
        checks['single_executor']=fact('FAIL','EXECUTOR_AND_LOCK') if not stopped or not lock_free or readonly.get('ActiveState')!='active' or readonly.get('MainPID') in ('0',None) else scan
        port=run(['ss','-H','-ltn','sport = :8765'])
        if port.returncode: raise ValueError()
        free=not port.stdout.strip()
        owned_readonly=False
        if not free and readonly.get('MainPID') not in ('0',None) and readonly.get('ActiveState')=='active':
            # A listening port must be attributable to the exact readonly process.
            listeners=run(['ss','-H','-ltnp','sport = :8765'])
            from lp_v2.live_control.listener import owned
            owned_readonly=owned(profile,readonly,listeners)
        checks['port']=fact('PASS' if free or owned_readonly else 'FAIL','PORT_OWNER')
    except Exception:
        for name in ('service','single_executor','port'):
            checks.setdefault(name,fact('UNKNOWN','SERVICE_EVIDENCE_MISSING'))
    return checks


def legacy_service_check():
    try:
        response=run(['systemctl','show','open-trader-prediction.service','--property=ActiveState,UnitFileState'])
        if response.returncode:raise ValueError()
        values=dict(line.split('=',1) for line in response.stdout.splitlines() if '=' in line)
        if not values.get('ActiveState') or not values.get('UnitFileState'):raise ValueError()
        return fact('PASS' if values['ActiveState']=='inactive' and values['UnitFileState']=='disabled' else 'FAIL','LEGACY_SERVICE_HANDOFF')
    except Exception:
        return fact('UNKNOWN','LEGACY_SERVICE_UNAVAILABLE')


def local_executor_check():
    from time import monotonic
    started=monotonic()
    try:
        mounts=Path('/proc/mounts').read_text()
        if len(mounts)>1048576:raise ValueError()
        proc=[line.split() for line in mounts.splitlines() if len(line.split())>=4 and line.split()[1]=='/proc']
        if len(proc)!=1 or proc[0][2]!='proc' or any(option.startswith('hidepid=') and option not in ('hidepid=0','hidepid=off') for option in proc[0][3].split(',')):
            raise ValueError('process visibility incomplete')
        count=0
        for path in Path('/proc').iterdir():
            if not path.name.isdecimal():continue
            count+=1
            if count>4096 or monotonic()-started>2:raise ValueError()
            try:
                raw=(path/'cmdline').read_bytes()
                if len(raw)>65536:raise ValueError()
                args=[value.decode('utf-8') for value in raw.split(b'\0') if value]
            except OSError:
                # A race is harmless only with an independently absent PID directory.
                try:path.stat()
                except FileNotFoundError:continue
                raise
            if any(arg.endswith('/lp-v2') or arg in ('lp_v2','lp_v2.cli') for arg in args) and 'run' in args and '--live' in args:
                return fact('FAIL','COMPETING_LOCAL_EXECUTOR')
        if monotonic()-started>2:raise ValueError()
        return fact('PASS','LOCAL_EXECUTOR_HANDOFF')
    except Exception:
        return fact('UNKNOWN','LOCAL_EXECUTOR_SCAN_INCOMPLETE')


def observe(request):
    profile=request['profile'];state=Path(profile['files']['state_dir'])
    process=read_json(state/'process.json')
    record=read_json(state/'live-control-run.json')
    if record['profile']!=profile or record['run_id']!=request.get('run_id') or process['run_id']!=record['run_id']:
        raise ValueError()
    from lp_v2.live_process import process_identity
    identity=process_identity(process['pid'])
    live=system_properties(profile['services']['live_unit']);readonly=system_properties(profile['services']['readonly_unit'])
    from lp_v2.live_control.invocation import actual_invocation
    if identity is None or identity!=process['identity'] or str(process['pid'])!=live.get('MainPID') or live.get('ActiveState')!='active':
        raise ValueError('process continuity unavailable')
    derived=actual_invocation(profile,process['pid'],record['invocation']['argv'])
    if derived!=record['invocation']['snapshot'] or process_identity(process['pid'])!=identity:
        raise ValueError('file or process continuity unavailable')
    import httpx
    with httpx.Client(timeout=5,trust_env=False,follow_redirects=False) as client:
        response=client.get('http://127.0.0.1:8765/summary');response.raise_for_status();row=response.json()
    result=dict(schema_version=1,run_id=record['run_id'],executor_running=identity is not None and identity==process['identity']
                and str(process['pid'])==live.get('MainPID') and live.get('ActiveState')=='active',
                readonly_stopped=readonly.get('MainPID')=='0' and readonly.get('ActiveState')=='inactive',
                identity=derived['identity'],summary=safe_summary(row))

    result['startup_verification']=record_start_observation(request,result)
    return result


def record_start_observation(request,snapshot):
    """Persist the existing two-seed-round proof under the control record lock."""
    import fcntl,os,stat
    from lp_v2.live_control.profiles import atomic_json,timestamp
    from lp_v2.live_control.workflow import valid_round
    profile=request['profile'];state=Path(profile['files']['state_dir'])
    fd=None
    try:
        secure_directory(state)
        fd=os.open(state/'control.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError('control lock identity')
        fcntl.flock(fd,fcntl.LOCK_EX)
        record=read_json(state/'live-control-run.json');process=read_json(state/'process.json')
        if (record['profile']!=profile or record['run_id']!=request['run_id'] or process['run_id']!=record['run_id']
                or request.get('action_id',record['action_id'])!=record['action_id'] or not record['window_end']):
            raise ValueError('startup proof run mismatch')
        bound=dict(action_id=record['action_id'],run_id=record['run_id'],window_end=record['window_end'],
                   seed_market=profile['trading']['market_condition_id'])
        prior=record.get('startup_verified',{})
        if all(prior.get(key)==value for key,value in bound.items()) and prior.get('verified_rounds')==2:
            return dict(status='PASS',verified_rounds=2)
        moment=valid_round(profile,record['run_id'],snapshot,record['started_at'])
        samples=record.get('startup_samples',[])
        if moment is None:
            samples=[]
        elif not samples or all(new>timestamp(old) for new,old in zip(moment,samples[-1])):
            samples=[*samples,[value.isoformat() for value in moment]][-2:]
        record['startup_samples']=samples
        if len(samples)==2:
            record['startup_verified']=dict(bound,verified_rounds=2,moments=samples,verified_at=now())
        atomic_json(state/'live-control-run.json',record)
        return dict(status='PASS' if len(samples)==2 else 'PENDING',verified_rounds=len(samples))
    except Exception:
        return dict(status='UNKNOWN',verified_rounds=0)
    finally:
        if fd is not None:os.close(fd)


def start(request):
    from lp_v2.live_control.workflow import normalize_report
    from lp_v2.live_control.profiles import atomic_json,timestamp
    import time
    profile=request['profile'];state=Path(profile['files']['state_dir'])
    report=normalize_report(profile,check(profile))
    if report['status']!='PASS': return report
    if not confirmed(request,'on'): return dict(status='UNKNOWN',reason='CONFIRMATION_EXPIRED')
    existing=read_json(state/'process.json') if (state/'process.json').exists() else {}
    previous=existing.get('run_id')
    seq=max((seq for seq,_ in journal_rows(profile,'occupation')),default=0) if journal_present(profile) else 0
    from lp_v2.rotation_state import journal_start_sequences
    journal_seqs=journal_start_sequences(profile)
    if (state/'journal.sqlite').exists() and not journal_seqs:
        journal_seqs[profile['trading']['market_condition_id']]=seq
    from lp_v2.live_control.invocation import binding,file_snapshot
    invocation=dict(argv=binding(profile,system_properties(profile['services']['live_unit'],prove_exec=True),env_values(profile),run),
                    snapshot=file_snapshot(profile))
    record=dict(profile=profile,run_id=None,started_at=now(),journal_seq=seq,journal_seqs=journal_seqs,action_id=request['action_id'],window_end=request['window_end'],invocation=invocation)
    atomic_json(state/'live-control-run.json',record)
    if run(['systemctl','stop',profile['services']['readonly_unit']]).returncode:
        return dict(status='UNKNOWN',reason='READONLY_STOP_FAILED')
    # Accepted start may run even if its response is lost: the action ledger was persisted first.
    response=run(['systemctl','start',profile['services']['live_unit']],timeout=30)
    for _ in range(10):
        try:
            process=read_json(state/'process.json')
            from lp_v2.live_process import process_identity
            if process['run_id']!=previous and process_identity(process['pid']) is not None and process_identity(process['pid'])==process['identity']:
                latest=read_json(state/'live-control-run.json')
                if latest['profile']!=profile or latest['action_id']!=request['action_id']:
                    raise ValueError('on record changed during start')
                record=dict(latest,run_id=process['run_id'])
                if 'lease_id' in process:
                    from lp_v2.rotation_state import physical_binding
                    record['process_binding']=physical_binding(process)
                atomic_json(state/'live-control-run.json',record)
                return dict(status='ACCEPTED',run_id=record['run_id'],started_at=record['started_at'])
        except Exception:
            pass
        time.sleep(1)
    return dict(status='UNKNOWN',reason='START_OUTCOME_UNKNOWN')



def import_profile(partial):
    from lp_v2.live_control.profiles import safe_name
    import re
    name=safe_name(partial['name']);root=partial['release']['root'];target=partial['target']
    if (not re.fullmatch(r'/[A-Za-z0-9_/-]+',root) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',target['ssh_host'])
            or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}',target['host_key_sha256'])):
        raise ValueError()
    profile=dict(schema_version=1,name=name,revision=1,target=target,release=dict(root=root),trading={},identity={},
        files=dict(signing_key='/etc/lp-v2/signing-key',l2_credentials='/etc/lp-v2/credentials.json',
                   relayer_credentials='/etc/lp-v2/relayer.json',run_env='/etc/lp-v2/run.env',state_dir=root+'/state'),
        services=dict(user='lp-v2',readonly_unit='lp-v2.service',live_unit='lp-v2-live.service',status_port=8765),
        metadata=dict(updated_at=now(),sources={}))
    live=system_properties('lp-v2-live.service')
    if (live.get('User')!='lp-v2' or profile['files']['run_env'] not in live.get('EnvironmentFiles','')
            or root+'/current/deploy/run-live' not in live.get('ExecStart','')
            or profile['files']['state_dir'] not in live.get('ExecStop','')):
        raise ValueError('explicit import locations do not match effective service')
    sha=(Path(root)/'current/COMPLETE').read_text().strip()
    if not re.fullmatch('[0-9a-f]{40}',sha): raise ValueError()
    profile['release']['expected_sha']=sha
    values=env_values(profile)
    if values.get('LP_V2_CODE_SHA') not in (None,sha):
        raise ValueError('conflicting version sources')
    for key,field in [('LP_V2_MARKET','market_condition_id'),('LP_V2_CAPITAL_USDC','capital_usdc')]:
        if key in values: profile['trading'][field]=values[key]
    credentials=read_json(profile['files']['l2_credentials'])
    for field in ('signer_address','wallet_address','signature_type'):
        if field in credentials: profile['identity'][field]=credentials[field]
    from lp_v2.live_control.profiles import FIELDS
    for group,fields in FIELDS.items():
        for field in fields:
            if field in profile[group]:
                source={'target':'target.ssh','release':'target.release','trading':'target.run_env','identity':'target.identity','files':'target.files','services':'target.unit'}[group]
                profile['metadata']['sources'][group+'.'+field]=dict(source=source,observed_at=now())
    validate(profile)
    return dict(profile=profile,status='IMPORTED',scope='configuration_only')


def safe_public(profile,raw):
    from lp_v2.live_control.workflow import public_details
    from lp_v2.live_control.profiles import timestamp
    from decimal import Decimal
    import re
    raw=raw if isinstance(raw,dict) else {}
    result=dict(scope='public_market' if raw.get('scope')=='public_market' else 'UNKNOWN',
                status=raw.get('status') if raw.get('status') in ('PASS','BLOCKED','UNKNOWN') else 'UNKNOWN',
                checks=public_details(raw.get('checks'),raw.get('as_of')))
    if (raw.get('code_sha')!=profile['release']['expected_sha'] or raw.get('market')!=profile['trading']['market_condition_id']
            or raw.get('capital_usdc')!=profile['trading']['capital_usdc']):
        result['checks']['opening_eligibility'].pop('details',None)
    for key,pattern in [('code_sha',r'[0-9a-f]{40}'),('market',r'0x[0-9a-fA-F]{64}')]:
        value=raw.get(key)
        if isinstance(value,str) and re.fullmatch(pattern,value):result[key]=value
    try:
        capital=raw.get('capital_usdc')
        if isinstance(capital,str) and Decimal(capital).is_finite() and Decimal(capital)>0:
            result['capital_usdc']=capital
    except Exception:pass
    try:result['as_of']=timestamp(raw.get('as_of')).isoformat()
    except Exception:pass
    return result


def safe_summary(raw):
    import re
    from decimal import Decimal
    from lp_v2.live_control.profiles import timestamp
    def stamp(value):
        try: return timestamp(value).isoformat()
        except Exception: return 'UNKNOWN'
    def matching(value,pattern):
        return value if isinstance(value,str) and re.fullmatch(pattern,value) else 'UNKNOWN'
    try:
        capital=Decimal(raw['capital_usdc'])
        capital=str(capital) if capital.is_finite() and capital>0 else 'UNKNOWN'
        return dict(mode=raw['mode'] if raw['mode'] in ('live','read_only') else 'UNKNOWN',as_of=stamp(raw.get('as_of')),
            capital_usdc=capital,run=dict(code_sha=matching(raw['run'].get('code_sha'),r'[0-9a-f]{40}'),
                wallet_tail=matching(raw['run'].get('wallet_tail'),r'[0-9a-fA-F]{4}')),
            accounting=dict(status='SUCCESS' if raw.get('accounting',{}).get('status')=='SUCCESS' else 'UNKNOWN',
                as_of=stamp(raw.get('accounting',{}).get('as_of'))),freshness=dict(decision_as_of=stamp(raw.get('freshness',{}).get('decision_as_of'))),
            markets=[dict(condition_id=matching(row.get('condition_id'),r'0x[0-9a-fA-F]{64}'),
                          eligible=row.get('eligible') is True,skip_reason=None if row.get('skip_reason') is None else 'INELIGIBLE') for row in raw['markets']])
    except Exception:
        return dict(mode='UNKNOWN')



def validate_mutation_paths(request):
    profile=request['profile']
    state=Path(profile['files']['state_dir']);secure_directory(state)
    paths=[state]
    if request['action']=='prepare':paths.append(Path(profile['files']['run_env']))
    for path in paths:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError('symlink path refused')
    if request['action']=='prepare':
        with owner_file(profile['files']['run_env']):pass
        effective=system_properties('lp-v2-live.service')
        if profile['files']['run_env'] not in effective.get('EnvironmentFiles',''):
            raise ValueError('environment reference differs')


def main(*,stdin=None,stdout=None):
    stdin=stdin or sys.stdin; stdout=stdout or sys.stdout
    try:
        request=json.loads(stdin.read(131073))
        if type(request.get('schema_version')) is not int or request['schema_version']!=1 or request.get('action') not in ('check','prepare','off','result','on','observe','import'):
            raise ValueError()
        if set(request)-{'schema_version','action','profile','action_id','confirmation','window_end','changes','run_id','expected_action'}:
            raise ValueError()
        profile=request['profile']
        if request['action']!='import': validate(profile,complete=True)
        from lp_v2.live_control.privileges import application_identity
        with application_identity():
            if request['action']=='import': result=import_profile(profile)
            elif request['action']=='check': result=check(profile)
            elif request['action']=='result': result=action_result(request)
            elif request['action']=='observe': result=observe(request)
            else: result=mutate(request)
    except Exception:
        result=dict(schema_version=1,status='UNKNOWN',reason='HELPER_REQUEST_INVALID')
    print(json.dumps(result),file=stdout)
    return 0 if result.get('status')=='PASS' else 2


if __name__=='__main__':
    raise SystemExit(main())
