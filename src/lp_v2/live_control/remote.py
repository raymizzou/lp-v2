"""Finite SSH protocol using a pin already present in the operator's SSH trust."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from lp_v2.live_control.profiles import ProfileError, config_root, secure_directory, validate


def _run(argv, **kw):
    return subprocess.run(argv,text=True,capture_output=True,timeout=180,check=False,**kw)


def request(profile, action, **payload):
    if action not in ('import','check','on','off','result','observe','prepare'):
        raise ProfileError('REQUEST_INVALID')
    host=profile.get('target',{}).get('ssh_host')
    root=profile.get('release',{}).get('root')
    if not isinstance(host,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',host):
        raise ProfileError('PROFILE_INVALID')
    if action!='import':
        validate(profile,complete=True)
    if not isinstance(root,str) or not re.fullmatch(r'/[A-Za-z0-9_/-]+',root):
        raise ProfileError('PROFILE_INVALID')
    config=_run(['ssh','-G','--',host])
    if config.returncode:
        raise ProfileError('SSH_UNAVAILABLE')
    settings={line.split(' ',1)[0]:line.split(' ',1)[1] for line in config.stdout.splitlines() if ' ' in line}
    lookup=settings.get('hostkeyalias') or settings.get('hostname')
    port=settings.get('port','22')
    if not lookup or not port.isdecimal():
        raise ProfileError('SSH_IDENTITY_MISMATCH')
    lookup=lookup if port=='22' else '['+lookup+']:'+port
    keys=[]
    # Lookup only existing files declared by OpenSSH; never scan or accept a key.
    for filename in settings.get('userknownhostsfile','').split():
        if filename=='none':
            continue
        result=_run(['ssh-keygen','-F',lookup,'-f',os.path.expanduser(filename)])
        if result.returncode not in (0,1):
            continue
        for line in result.stdout.splitlines():
            parts=line.split()
            if len(parts)!=3 or parts[1]!='ssh-ed25519':
                continue
            fingerprint=_run(['ssh-keygen','-lf','-','-E','sha256'],input=line+'\n')
            values=fingerprint.stdout.split()
            if fingerprint.returncode==0 and len(values)>=2:
                keys.append((values[1],parts[1],parts[2]))
    expected=profile['target'].get('host_key_sha256')
    matches=[key for key in keys if expected is None or key[0]==expected]
    if len(matches)!=1:
        raise ProfileError('SSH_IDENTITY_MISMATCH')
    pin,kind,key=matches[0]
    base=config_root(); base.mkdir(mode=0o700,parents=True,exist_ok=True); secure_directory(base)
    with tempfile.TemporaryDirectory(prefix='.ssh-pin-',dir=base) as temporary:
        known=Path(temporary)/'known_hosts'
        # Match the actual presented host regardless of configured HostKeyAlias.
        known.write_text('* '+kind+' '+key+'\n'); known.chmod(0o600)
        import shlex
        from lp_v2.live_control.privileges import BOOTSTRAP
        entry=shlex.join(['sudo','-n','--','/usr/bin/python3','-I','-c',BOOTSTRAP,root,profile['release'].get('expected_sha','-')])
        argv=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UpdateHostKeys=no',
              '-o','VerifyHostKeyDNS=no','-o','HostKeyAlgorithms=ssh-ed25519',
              '-o','GlobalKnownHostsFile=/dev/null','-o','UserKnownHostsFile='+str(known),
              '-o','ControlMaster=no','-o','ControlPath=none','-o','ConnectTimeout=10',
              '--',host,entry]
        wire_profile=profile
        if action=='import':
            from copy import deepcopy
            wire_profile=deepcopy(profile);wire_profile['target']['host_key_sha256']=pin
        response=_run(argv,input=json.dumps(dict(schema_version=1,action=action,profile=wire_profile,**payload)))
    if response.returncode not in (0,2):
        raise ProfileError('REMOTE_OUTCOME_UNKNOWN')
    result=json.loads(response.stdout)
    if action=='import' and result.get('profile',{}).get('target',{}).get('host_key_sha256')!=pin:
        raise ProfileError('SSH_IDENTITY_MISMATCH')
    return result
