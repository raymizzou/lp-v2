"""Linux executor lease and pidfd stop: PID files alone never authorize a signal."""
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
from time import monotonic as real_monotonic, sleep
from uuid import uuid4

from lp_v2.cli import owner_file


def _directory(path):
    path=Path(path)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('owned 0700 state directory required')
    return path


def _write(path,payload):
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as output:
            json.dump(payload,output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def process_identity(pid):
    try:
        if type(pid) is not int or pid <= 1 or Path(f'/proc/{pid}').stat().st_uid != os.getuid():
            return None
        command=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if not any(arg.endswith(b'/lp-v2') or arg in (b'lp_v2.cli',b'lp_v2') for arg in command):
            return None
        fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        start=fields[19]
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return boot+':'+start
    except (OSError,IndexError):
        return None


class ProcessLock:
    def __init__(self,path):
        self.path=Path(path)
        self.fd=None

    def __enter__(self):
        self.path.mkdir(mode=0o700,exist_ok=True)
        _directory(self.path)
        self.fd=os.open(self.path/'executor.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(self.fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('invalid lock file')
            fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            identity=process_identity(os.getpid())
            if identity is None:
                raise ValueError('Linux process identity unavailable')
            self.record=dict(pid=os.getpid(),identity=identity,run_id=uuid4().hex)
            _write(self.path/'process.json',self.record)
            return self
        except BaseException:
            os.close(self.fd)
            self.fd=None
            raise

    def finish(self,code):
        _write(self.path/'stop-result.json',dict(run_id=self.record['run_id'],exit_code=code))

    def __exit__(self,*args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd=None


def stop_process(path,*,timeout_s=110,monotonic=real_monotonic,wait=sleep):
    pidfd=None
    try:
        path=_directory(path)
        with owner_file(path/'executor.lock') as lock:
            try:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                # Idempotent ExecStop cleanup; prior drain evidence remains untouched.
                return 0
            except BlockingIOError:
                pass
            with owner_file(path/'process.json') as source:
                record=json.load(source)
            identity=process_identity(record['pid'])
            if not identity or identity != record['identity'] or not isinstance(record['run_id'],str):
                return 1
            pidfd=os.pidfd_open(record['pid'],0)
            if process_identity(record['pid']) != identity:
                return 1
            signal.pidfd_send_signal(pidfd,signal.SIGTERM,None,0)
            deadline=monotonic()+timeout_s
            while monotonic() <= deadline:
                try:
                    with owner_file(path/'stop-result.json') as source:
                        result=json.load(source)
                    if result['run_id'] == record['run_id'] and type(result['exit_code']) is int:
                        return result['exit_code']
                except (OSError,ValueError,KeyError):
                    pass
                if monotonic() >= deadline:
                    break
                wait(min(1,deadline-monotonic()))
    except (OSError,ValueError,KeyError,AttributeError,TypeError):
        pass
    finally:
        if pidfd is not None:
            os.close(pidfd)
    return 1
