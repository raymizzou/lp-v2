"""Bounded readonly listener proof for the shipped direct/flock invocations."""
from pathlib import Path
import os
import re
from lp_v2.live_control.invocation import command, canonical


def process(pid,group):
    if not re.fullmatch('[1-9][0-9]{0,9}',pid) or int(pid)<=1:raise ValueError('pid unavailable')
    base=Path('/proc')/pid
    uid=base.stat().st_uid
    raw=(base/'cmdline').read_bytes()
    fields=(base/'stat').read_text()
    cgroup=(base/'cgroup').read_text()
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if (uid!=os.getuid() or not raw or len(raw)>16384 or not raw.endswith(b'\0')
            or len(fields)>16384 or len(cgroup)>4096 or cgroup.strip()!='0::'+group or not boot or len(boot)>128):
        raise ValueError('process evidence unavailable')
    start=fields.rsplit(')',1)[1].split()[19]
    if not start.isdecimal():raise ValueError('process identity unavailable')
    return (uid,raw,boot,start,cgroup)


def owned(profile,readonly,listeners):
    try:
        group='/system.slice/lp-v2.service'
        if readonly.get('ControlGroup')!=group or readonly.get('User')!='lp-v2':return False
        lines=listeners.stdout.strip().splitlines()
        if listeners.returncode or len(lines)!=1 or '127.0.0.1:8765' not in lines[0].split():return False
        pids=re.findall(r'pid=([0-9]+),',lines[0])
        if len(pids)!=1:return False
        pid=pids[0];main=readonly['MainPID']
        root=Path(profile['release']['root']);release=root/'releases'/profile['release']['expected_sha']
        expected=[str(release/'.venv/bin/lp-v2'),'run','--capital-usdc',profile['trading']['capital_usdc'],
            '--code-sha',profile['release']['expected_sha'],'--credentials-file','/etc/lp-v2/credentials.json']
        if canonical(profile['files']['l2_credentials'])!=canonical('/etc/lp-v2/credentials.json'):return False
        wrapper=['/usr/bin/flock','-n',str(root/'runtime.lock'),str(root/'current/run-readonly')]
        effective=command(readonly['ExecStart'])
        before=process(main,group)
        args=before[1][:-1].decode().split('\0')
        children=None
        if effective==wrapper:
            if args!=wrapper or pid==main:return False
            path=Path(f'/proc/{main}/task/{main}/children')
            children=path.read_text()
            if len(children)>4096 or children.split()!=[pid]:return False
        elif effective not in (expected,[str(root/'current/run-readonly')]) or pid!=main:return False
        listener=process(pid,group)
        args=listener[1][:-1].decode().split('\0')
        if args and args[0] in (str(release/'.venv/bin/python'),str(release/'.venv/bin/python3')):args=args[1:]
        if args!=expected:return False
        if listener!=process(pid,group) or before!=process(main,group):return False
        if children is not None and children!=path.read_text():return False
        return True
    except (OSError,ValueError,KeyError,IndexError,UnicodeError):
        return False
