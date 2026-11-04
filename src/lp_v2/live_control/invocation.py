"""Finite interpreter for the pinned shipped launcher; never evaluates shell text."""
from pathlib import Path
import hashlib
import re
import shlex

LAUNCHER_SHA256='3b34120b492c53122b4de0b241355b73e998fda2bebdac3257177c3c0d6a61f9'
REFS={'signing_key':'/etc/lp-v2/signing-key','l2_credentials':'/etc/lp-v2/credentials.json',
      'relayer_credentials':'/etc/lp-v2/relayer.json','state_dir':'/opt/lp-v2/state'}


def canonical(value):
    return str(Path(value).resolve(strict=True))


def command(value):
    # systemd's show format, or a single plain command. No multiple commands,
    # quoting/escaping extensions or ignored failures are inferred.
    if value.startswith('{'):
        match=re.fullmatch(r'\{ path=([^;]+) ; argv\[\]=([^;]+) ; ignore_errors=no ;[^{}]*\}',value)
        if not match:raise ValueError('unsupported effective command')
        args=shlex.split(match[2])
        if not args or args[0]!=match[1]:raise ValueError('command path differs')
        return args
    if any(c in value for c in '{};\n'):raise ValueError('ambiguous command')
    return shlex.split(value)


def expected_argv(profile,values):
    release=Path(profile['release']['root'])/'releases'/profile['release']['expected_sha']
    return [str(release/'.venv/bin/lp-v2'),'run','--live',
        '--capital-usdc',values.get('LP_V2_CAPITAL_USDC',''),'--market',values.get('LP_V2_MARKET',''),
        '--code-sha',values.get('LP_V2_CODE_SHA',''),'--private-key-file',REFS['signing_key'],
        '--credentials-file',REFS['l2_credentials'],'--relayer-credentials-file',REFS['relayer_credentials'],
        '--state-dir',REFS['state_dir']]


def binding(profile,live,values,run):
    current=Path(profile['release']['root'])/'current'
    if set(values)-{'LP_V2_CAPITAL_USDC','LP_V2_MARKET','LP_V2_CODE_SHA'}:raise ValueError('unsupported environment input')
    expected={'User':'lp-v2','Group':'lp-v2','Type':'simple','UMask':'0077','NoNewPrivileges':'yes',
        'Restart':'no','DynamicUser':'no','WorkingDirectory':str(current),'TimeoutStopUSec':'2min 30s'}
    if any(live.get(k)!=v for k,v in expected.items()):raise ValueError('effective service differs')
    if live.get('KillSignal') not in ('15','SIGTERM'):raise ValueError('signal differs')
    empty=('ExecStartPre','ExecStartPost','ExecCondition','ExecStopPost','ExecReload','Environment','PassEnvironment',
        'UnsetEnvironment','RootDirectory','RootImage','PAMName','ExecSearchPath','SupplementaryGroups')
    if any(live.get(k)!='' for k in empty):raise ValueError('unsupported service override')
    if command(live.get('ExecStart',''))!=[str(current/'deploy/run-live')]:raise ValueError('launcher differs')
    stop=command(live.get('ExecStop',''))
    if len(stop)!=4 or stop[:3]!=[str(current/'.venv/bin/lp-v2'),'stop','--state-dir'] or canonical(stop[3])!=canonical(REFS['state_dir']):
        raise ValueError('stop differs')
    env=live.get('EnvironmentFiles','')
    if env.endswith(' (ignore_errors=no)'):env=env[:-19]
    if env!=profile['files']['run_env']:raise ValueError('environment reference differs')
    if any(canonical(path)!=canonical(profile['files'][name]) for name,path in REFS.items()):raise ValueError('file reference differs')
    from lp_v2.live_control.source import read
    reference=read(profile,['show',profile['release']['expected_sha']+':deploy/run-live'],run)
    installed=(current/'deploy/run-live').read_bytes()
    if reference.returncode or installed!=reference.stdout.encode() or hashlib.sha256(installed).hexdigest()!=LAUNCHER_SHA256:
        raise ValueError('unsupported launcher bytes')
    return expected_argv(profile,values)


def file_snapshot(profile):
    """Only metadata and derived public identity survive the protected reads."""
    import json
    import os
    from eth_account import Account
    from lp_v2.cli import owner_file
    from lp_v2.readonly_sdk import L2Credentials
    from polymarket._internal.wallet import classify_wallet_type
    from polymarket.environments import PRODUCTION
    metadata={};values={}
    def stamp(info):
        return [info.st_dev,info.st_ino,info.st_uid,info.st_gid,info.st_mode,info.st_size,info.st_mtime_ns,info.st_ctime_ns]
    for key in ('signing_key','l2_credentials','relayer_credentials','run_env'):
        path=profile['files'][key]
        with owner_file(path) as source:
            before=stamp(os.fstat(source.fileno()));text=source.read();after=stamp(os.fstat(source.fileno()))
        with owner_file(path) as source:
            if before!=after or before!=stamp(os.fstat(source.fileno())):raise ValueError('file changed')
        metadata[key]=before
        if key!='run_env':values[key]=text
    signer=Account.from_key(values['signing_key'].strip()).address
    credentials=L2Credentials(**json.loads(values['l2_credentials']));relayer=json.loads(values['relayer_credentials'])
    if (type(credentials.signature_type) is not int or credentials.signature_type!=1
            or signer.lower()!=credentials.signer_address.lower()
            or classify_wallet_type(signer=signer,wallet=credentials.wallet_address,config=PRODUCTION.wallet_derivation)!='POLY_PROXY'):
        raise ValueError('derived identity differs')
    from lp_v2.relayer_credentials import parse_relayer_credentials
    parse_relayer_credentials(relayer,signer)
    identity=dict(signer_address=signer,wallet_address=credentials.wallet_address,signature_type=1)
    from lp_v2.live_control.identity import same_identity
    if not same_identity(identity,profile['identity']):raise ValueError('full frozen identity differs')
    for key,before in metadata.items():
        with owner_file(profile['files'][key]) as source:
            if before!=stamp(os.fstat(source.fileno())):raise ValueError('file continuity unavailable')
    return dict(files=metadata,identity=identity)


def actual_invocation(profile,pid,expected):
    raw=Path(f'/proc/{pid}/cmdline').read_bytes()
    if not raw or len(raw)>16384 or not raw.endswith(b'\0'):raise ValueError('process arguments unavailable')
    args=[value.decode('utf-8') for value in raw[:-1].split(b'\0')]
    release=Path(profile['release']['root'])/'releases'/profile['release']['expected_sha']
    if args and args[0] in (str(release/'.venv/bin/python'),str(release/'.venv/bin/python3')):args=args[1:]
    if args!=expected:raise ValueError('process invocation differs')
    # Derive from explicit actual argument references, after exact invocation match.
    actual=dict(profile,files=dict(profile['files']))
    for flag,key in [('--private-key-file','signing_key'),('--credentials-file','l2_credentials'),('--relayer-credentials-file','relayer_credentials')]:
        actual['files'][key]=args[args.index(flag)+1]
    return file_snapshot(actual)
