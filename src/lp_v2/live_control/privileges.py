"""Short-lived operator control, with application work under the service identity.

No policy is installed. A successful existing operator sudo entry is required.
The saved root identity is used only by the fixed systemctl allowlist below.
"""
from contextvars import ContextVar
from contextlib import contextmanager
import os
import pwd
import subprocess

getresuid=getattr(os,'getresuid',None)
getresgid=getattr(os,'getresgid',None)
setresuid=getattr(os,'setresuid',None)
setresgid=getattr(os,'setresgid',None)
geteuid=os.geteuid
seteuid=os.seteuid
getgroups=os.getgroups
setgroups=os.setgroups
_current=ContextVar('live_control_operator',default=None)
PROPERTIES='--property=MainPID,ActiveState,User,ControlGroup,ExecStart,ExecStop,EnvironmentFiles,Conflicts,TimeoutStopUSec,Restart,UnitFileState,Group,WorkingDirectory,Type,UMask,NoNewPrivileges,KillSignal,ExecStartPre,ExecStartPost,ExecCondition,ExecStopPost,ExecReload,Environment,PassEnvironment,UnsetEnvironment,RootDirectory,RootImage,DynamicUser,PAMName,ExecSearchPath,SupplementaryGroups'


class AuthorityUnavailable(ValueError):
    pass


def service_account():
    return pwd.getpwnam('lp-v2')


@contextmanager
def application_identity():
    if geteuid()!=0 or any(call is None for call in (getresuid,getresgid,setresuid,setresgid)):
        raise AuthorityUnavailable('OPERATOR_AUTHORITY_UNAVAILABLE')
    account=service_account()
    if account.pw_uid<=0 or account.pw_gid<0:
        raise AuthorityUnavailable('SERVICE_IDENTITY_INVALID')
    original_uids=getresuid();original_gids=getresgid();groups=getgroups()
    token=None
    try:
        setgroups([])
        setresgid(account.pw_gid,account.pw_gid,0)
        setresuid(account.pw_uid,account.pw_uid,0)
        if geteuid()!=account.pw_uid:
            raise AuthorityUnavailable('SERVICE_IDENTITY_UNAVAILABLE')
        token=_current.set(account.pw_uid)
        yield
    finally:
        if token is not None:_current.reset(token)
        seteuid(0)
        setresgid(*original_gids)
        setgroups(groups)
        setresuid(*original_uids)


def operator_ready():
    uid=_current.get()
    return uid is not None and geteuid()==uid


def systemctl(argv,*,timeout):
    # No data-driven command, unit, options, environment or shell execution.
    if argv==['systemctl','show','open-trader-prediction.service','--property=ActiveState,UnitFileState'] and operator_ready():
        return subprocess.run(argv,text=True,capture_output=True,timeout=timeout,check=False,
            env=dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin',LANG='C',SYSTEMD_PAGER='cat',SYSTEMD_COLORS='0'))
    if (not operator_ready() or len(argv)<3 or argv[0]!='systemctl'
            or argv[2] not in ('lp-v2.service','lp-v2-live.service')
            or not ((argv[1] in ('start','stop') and len(argv)==3)
                    or (argv[1]=='show' and argv[3:]==[PROPERTIES]))):
        raise AuthorityUnavailable('SERVICE_CONTROL_REFUSED')
    uid=_current.get()
    try:
        seteuid(0)
        return subprocess.run(argv,text=True,capture_output=True,timeout=timeout,check=False,
            env=dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin',LANG='C',SYSTEMD_PAGER='cat',SYSTEMD_COLORS='0'))
    finally:
        seteuid(uid)


# Passed literally to the system interpreter: inspect immutable deployment metadata
# before importing/executing any release-provided privileged Python code. Stdin
# remains untouched for the real target dispatcher. No application secret is read.
BOOTSTRAP = r'''
import os, re, stat, sys
from pathlib import Path
if os.geteuid() != 0:
    raise PermissionError('operator authority required')
root = Path(sys.argv[1])
expected = sys.argv[2]
def immutable(path):
    for node in (path, *path.parents):
        info = node.lstat()
        if (info.st_uid != 0 or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise PermissionError('immutable root-owned deployment required')
immutable(root)
link = root / 'current'
if link.lstat().st_uid != 0 or not link.is_symlink():
    raise PermissionError('owned release selector required')
release = link.resolve(strict=True)
if (not re.fullmatch('[0-9a-f]{40}', release.name)
        or release != root / 'releases' / release.name
        or (expected != '-' and release.name != expected)):
    raise PermissionError('pinned release required')
immutable(release)
for node in release.rglob('*'):
    if node.is_symlink():
        if node.lstat().st_uid != 0:
            raise PermissionError('owned link required')
        destination = node.resolve(strict=True)
        if node.is_dir():
            destination.relative_to(release)
        immutable(destination)
    else:
        immutable(node)
if (release / 'COMPLETE').read_text().strip() != release.name:
    raise PermissionError('complete release required')
python = release / '.venv/bin/python'
immutable(python.resolve(strict=True))
os.execv(str(python), [str(python), '-I', '-m', 'lp_v2.live_control.host'])
'''


def empty_exec_property(unit,prop):
    """Finite unprivileged D-Bus proof for systemctl's omitted empty arrays."""
    import json
    objects={'lp-v2.service':'/org/freedesktop/systemd1/unit/lp_2dv2_2eservice',
             'lp-v2-live.service':'/org/freedesktop/systemd1/unit/lp_2dv2_2dlive_2eservice'}
    if unit not in objects or prop not in ('ExecStartPre','ExecStartPost','ExecCondition','ExecStopPost','ExecReload'):
        raise ValueError('unsupported property')
    result=subprocess.run(['busctl','--json=short','--system','get-property','org.freedesktop.systemd1',
        objects[unit],'org.freedesktop.systemd1.Service',prop],text=True,capture_output=True,timeout=15,check=False,
        env=dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin',LANG='C'))
    if result.returncode or len(result.stdout)>4096:raise ValueError('property unavailable')
    data=json.loads(result.stdout)
    if data!={'type':'a(sasbttttuii)','data':[]}:raise ValueError('property not proven empty')
