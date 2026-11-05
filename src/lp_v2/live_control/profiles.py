"""Local named targets; profile data never constitutes live authorization."""
import json
import os
from pathlib import Path


class ProfileError(ValueError):
    pass


def config_root():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'lp-v2'


def read_json(path):
    from lp_v2.cli import owner_file
    with owner_file(path) as source:
        return json.load(source)


def load(name=None):
    root=config_root()
    secure_directory(root); secure_directory(root/'profiles')
    if name is None:
        index=read_json(root/'config.json')
        if type(index.get('schema_version')) is not int or index.get('schema_version') != 1 or set(index)!={'schema_version','default_profile'}:
            raise ProfileError('INDEX_INVALID')
        name=index['default_profile']
    safe_name(name)
    profile=read_json(root/'profiles'/f'{name}.json')
    validate(profile)
    if profile['name']!=name:
        raise ProfileError('PROFILE_INVALID')
    return profile


def now():
    from datetime import datetime, UTC
    return datetime.now(UTC).isoformat()


def atomic_json(path, value):
    import tempfile
    fd, temporary=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w') as output:
            json.dump(value,output); output.flush(); os.fsync(output.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save(profile, *, default=False, expected=None):
    import fcntl
    import stat
    root=config_root(); root.mkdir(mode=0o700,parents=True,exist_ok=True)
    directory=root/'profiles'; directory.mkdir(mode=0o700,exist_ok=True)
    secure_directory(root); secure_directory(directory)
    validate(profile,complete=default)
    path=directory/(safe_name(profile['name'])+'.json')
    fd=os.open(root/'profile.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
            raise ProfileError('PROFILE_INVALID')
        fcntl.flock(fd,fcntl.LOCK_EX)
        current=read_json(path) if path.exists() or path.is_symlink() else None
        if current!=expected:
            raise ProfileError('REVISION_CONFLICT')
        profile=dict(profile)
        profile['revision']=1 if current is None else current['revision']+1
        entries=[(path,profile)]
        if default:
            entries.append((root/'config.json',dict(schema_version=1,default_profile=profile['name'])))
        original={p:read_json(p) if p.exists() or p.is_symlink() else None for p,_ in entries}
        raw={p:p.read_bytes() if original[p] is not None else None for p,_ in entries}
        for p in original:
            if original[p] is not None:
                atomic_bytes(p.with_name(p.name+'.bak'),raw[p])
        applied=[]
        try:
            for p,value in entries:
                atomic_json(p,value); applied.append(p)
        except BaseException:
            for p in reversed(applied):
                if raw[p] is None:
                    p.unlink()
                else:
                    # Restore via rename even if replace itself is unavailable.
                    restore=p.with_name('.'+p.name+'.restore')
                    atomic_bytes(restore,raw[p]); os.rename(restore,p)
            raise
        return profile
    finally:
        os.close(fd)


def atomic_bytes(path,data):
    import tempfile
    if path.exists() or path.is_symlink():
        from lp_v2.cli import owner_file
        with owner_file(path): pass  # validate existing backup inode without interpreting its bytes
    fd,name=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def set_default(name):
    profile=load(name); validate(profile,complete=True)
    print(json.dumps(dict(default_profile=name)))
    if input('Type save to change default: ')!='save':
        raise ProfileError('SAVE_CANCELLED')
    # A default change does not rewrite the profile or increment its revision.
    import fcntl
    fd=os.open(config_root()/'profile.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        if load(name)!=profile:
            raise ProfileError('REVISION_CONFLICT')
        path=config_root()/'config.json'
        if path.exists() or path.is_symlink():
            read_json(path); atomic_bytes(path.with_name('config.json.bak'),path.read_bytes())
        atomic_json(path,dict(schema_version=1,default_profile=name))
    finally:
        os.close(fd)
    return profile


def setup(name, host, *, capital=None, market=None, default=False, release_root='/opt/lp-v2'):
    from lp_v2.live_control.remote import request
    safe_name(name)
    host=host or input('Existing SSH Host alias: ')
    path=config_root()/'profiles'/(name+'.json')
    old=load(name) if path.exists() or path.is_symlink() else None
    target=dict(name=name,target=dict(ssh_host=host),release=dict(root=release_root))
    profile=request(target,'import')['profile']
    profile['name']=name
    stamp=now()
    for field, supplied in [('capital_usdc',capital),('market_condition_id',market)]:
        if supplied is None and not profile.get('trading',{}).get(field):
            supplied=input(field+': ')
        if supplied is not None:
            profile.setdefault('trading',{})[field]=supplied
            profile.setdefault('metadata',{}).setdefault('sources',{})['trading.'+field]=dict(source='operator',observed_at=stamp)
    profile['metadata']['updated_at']=stamp
    validate(profile)
    print(json.dumps(dict(previous=old,proposed=profile,path=str(config_root()/'profiles'/(name+'.json')))))
    if input('Type save to save local settings (not live authorization): ')!='save':
        raise ProfileError('SAVE_CANCELLED')
    return save(profile,default=default and not validate(profile),expected=old)


FIELDS={
    'target':('ssh_host','host_key_sha256'), 'release':('root','expected_sha'),
    'trading':('market_condition_id','capital_usdc'),
    'identity':('wallet_address','signer_address','signature_type'),
    'files':('signing_key','l2_credentials','relayer_credentials','run_env','state_dir'),
    'services':('user','readonly_unit','live_unit','status_port'),
}


def safe_name(name):
    import re
    if not isinstance(name,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',name):
        raise ProfileError('PROFILE_INVALID')
    return name


def secure_directory(path):
    import stat
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ProfileError('PROFILE_INVALID')


def timestamp(value):
    from datetime import datetime
    result=datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('timezone')
    return result


def validate(profile, *, complete=False):
    import re
    from decimal import Decimal
    try:
        if not isinstance(profile,dict) or set(profile)-{'schema_version','name','revision','metadata',*FIELDS}:
            raise ValueError()
        safe_name(profile['name'])
        if type(profile['schema_version']) is not int or profile['schema_version']!=1 or type(profile['revision']) is not int or profile['revision']<1:
            raise ValueError()
        missing=[]
        patterns={'target.ssh_host':r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',
            'target.host_key_sha256':r'SHA256:[A-Za-z0-9+/]{43}',
            'release.expected_sha':r'[0-9a-f]{40}', 'trading.market_condition_id':r'0x[0-9a-fA-F]{64}',
            'identity.wallet_address':r'0x[0-9a-fA-F]{40}', 'identity.signer_address':r'0x[0-9a-fA-F]{40}'}
        fixed={'identity.signature_type':1,'services.user':'lp-v2','services.readonly_unit':'lp-v2.service','services.live_unit':'lp-v2-live.service','services.status_port':8765}
        for group,fields in FIELDS.items():
            values=profile.get(group,{})
            if not isinstance(values,dict) or set(values)-set(fields):
                raise ValueError()
            for field in fields:
                key=group+'.'+field
                value=values.get(field)
                if value in (None,''):
                    missing.append(key); continue
                if key in fixed:
                    if type(value) is not type(fixed[key]) or value!=fixed[key]:
                        raise ValueError()
                elif not isinstance(value,str) or not value or any(c in value for c in ('<','>','\n','\r','\0')) or value.upper() in ('UNKNOWN','TODO','CHANGEME'):
                    raise ValueError()
                if key in patterns and not re.fullmatch(patterns[key],value):
                    raise ValueError()
                if group=='files' or key=='release.root':
                    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',value) or any(part in ('.','..','') for part in value.split('/')[1:]):
                        raise ValueError()
                if key=='trading.capital_usdc':
                    amount=Decimal(value)
                    if not amount.is_finite() or amount<=0:
                        raise ValueError()
        metadata=profile['metadata']
        if set(metadata)!={'updated_at','sources'} or not isinstance(metadata['sources'],dict):
            raise ValueError()
        timestamp(metadata['updated_at'])
        for key,value in metadata['sources'].items():
            if key not in {g+'.'+f for g,fs in FIELDS.items() for f in fs} or set(value)!={'source','observed_at'}:
                raise ValueError()
            if value['source'] not in (
                    'synthetic-fixture','operator','target.run_env','target.release',
                    'target.unit','target.identity','target.ssh','target.files',
                    'local.trusted_ssh_and_live_strict_handshake',
                    'target.current_release_and_four_source_hashes',
                    'target.l2_public_identity',
                    'target.lstat_and_service_environment',
                    'target.systemd_show_and_hash_verified_status_server'):
                raise ValueError()
            timestamp(value['observed_at'])
        if complete and missing:
            raise ProfileError('PROFILE_INCOMPLETE')
        return missing
    except ProfileError:
        raise
    except Exception:
        raise ProfileError('PROFILE_INVALID') from None


SET_FIELDS={'ssh_host':'target.ssh_host','release_root':'release.root','code_sha':'release.expected_sha',
    'market':'trading.market_condition_id','capital_usdc':'trading.capital_usdc',
    'signing_key_file':'files.signing_key','credentials_file':'files.l2_credentials',
    'relayer_credentials_file':'files.relayer_credentials','run_env':'files.run_env'}


def edit(name, changes):
    import copy
    old=load(name); updated=copy.deepcopy(old)
    for option,value in changes.items():
        if option not in SET_FIELDS or value is None:
            continue
        group,field=SET_FIELDS[option].split('.')
        updated[group][field]=value
        updated['metadata']['sources'][SET_FIELDS[option]]=dict(source='operator',observed_at=now())
    validate(updated)
    print(json.dumps(dict(previous=old,proposed=updated,impact='Local expectations only; check target differences before on.')))
    if input('Type save to save: ')!='save':
        raise ProfileError('SAVE_CANCELLED')
    updated['metadata']['updated_at']=now()
    return save(updated,expected=old)


def fingerprint(profile):
    import hashlib
    return hashlib.sha256(json.dumps(profile,sort_keys=True,separators=(',',':')).encode()).hexdigest()
