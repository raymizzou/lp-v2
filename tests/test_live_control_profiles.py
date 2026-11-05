"""Public CLI profile behavior, synthetic data and external boundaries only."""
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
from lp_v2.cli import main


@pytest.fixture
def profile():
    return json.loads((Path(__file__).parent/'data/live-control-profile.json').read_text())


@pytest.fixture
def config_home(tmp_path, monkeypatch, profile):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    root=tmp_path/'lp-v2'; root.mkdir(mode=0o700)
    profiles=root/'profiles'; profiles.mkdir(mode=0o700)
    path=profiles/'tokyo.json'; path.write_text(json.dumps(profile)); path.chmod(0o600)
    index=root/'config.json'; index.write_text('{"schema_version":1,"default_profile":"tokyo"}'); index.chmod(0o600)
    return root


def test_existing_v1_default_needs_no_setup(config_home, profile, monkeypatch, capsys):
    before={p:p.read_bytes() for p in (config_home/'profiles/tokyo.json',config_home/'config.json')}
    calls=[]
    def ssh(argv, **kw):
        if known_ssh(argv): return known_ssh(argv)
        request=json.loads(kw['input']); calls.append(request)
        return subprocess.CompletedProcess(argv,0,json.dumps({'schema_version':1,'status':'UNKNOWN','checks':{}}),'')
    monkeypatch.setattr(subprocess,'run',ssh)
    assert main(['live','profile','show','tokyo']) == 0
    shown=json.loads(capsys.readouterr().out)
    assert shown['profile']==profile and shown['missing']==[]
    expected_sources={
        'target':'local.trusted_ssh_and_live_strict_handshake',
        'release':'target.current_release_and_four_source_hashes',
        'trading':'target.run_env',
        'identity':'target.l2_public_identity',
        'files':'target.lstat_and_service_environment',
        'services':'target.systemd_show_and_hash_verified_status_server',
    }
    for field,provenance in shown['profile']['metadata']['sources'].items():
        assert provenance['source']==expected_sources[field.split('.')[0]]
    assert main(['live','check']) == 2
    capsys.readouterr()
    assert calls[-1]['profile']==profile and calls[-1]['action']=='check'
    assert all(p.read_bytes()==content for p,content in before.items())


def test_setup_imports_known_fields_without_remote_writes(config_home, profile, monkeypatch, capsys):
    (config_home/'profiles/tokyo.json').unlink()
    imported=copy.deepcopy(profile); del imported['trading']['capital_usdc']
    calls=[]
    def ssh(argv, **kw):
        if known_ssh(argv): return known_ssh(argv)
        request=json.loads(kw['input']); calls.append(request['action'])
        data={'profile':imported} if request['action']=='import' else {'status':'UNKNOWN','checks':{}}
        return subprocess.CompletedProcess(argv,0,json.dumps(data),'')
    answers=iter(['100','save'])
    monkeypatch.setattr('builtins.input',lambda prompt: next(answers))
    monkeypatch.setattr(subprocess,'run',ssh)
    assert main(['live','setup','--profile','tokyo','--ssh-host','synthetic-tokyo','--set-default'])==0
    result=json.loads((config_home/'profiles/tokyo.json').read_text())
    assert result['trading']['capital_usdc']=='100'
    assert result['identity']==profile['identity']
    assert result['metadata']['sources']['trading.capital_usdc']['source']=='operator'
    assert set(profile['metadata']['sources']) <= set(result['metadata']['sources'])
    assert json.loads((config_home/'config.json').read_text())['default_profile']=='tokyo'
    assert calls==['import','check']
    assert all((p.stat().st_mode & 0o777)==0o700 for p in (config_home,config_home/'profiles'))
    assert all((p.stat().st_mode & 0o777)==0o600 for p in (config_home/'config.json',config_home/'profiles/tokyo.json'))
    assert str(config_home/'profiles/tokyo.json') in capsys.readouterr().out


def test_profile_validation_rejects_unsafe_inputs(config_home, profile, monkeypatch, capsys):
    path=config_home/'profiles/tokyo.json'; index=config_home/'config.json'
    def no_remote(*a,**k):
        pytest.fail('invalid profile reached remote boundary')
    monkeypatch.setattr(subprocess,'run',no_remote)
    invalid=[]
    for key,value in [('schema_version',True),('schema_version',2),('revision',0),('name','../tokyo')]:
        item=copy.deepcopy(profile); item[key]=value; invalid.append(item)
    for group,field,value in [('trading','capital_usdc','NaN'),('trading','capital_usdc','<capital>'),('release','root','/opt/../tmp'),('target','ssh_host','host;touch /tmp/x'),('identity','signature_type',True)]:
        item=copy.deepcopy(profile); item[group][field]=value; invalid.append(item)
    item=copy.deepcopy(profile); del item['identity']['wallet_address']; invalid.append(item)
    for item in invalid:
        path.write_text(json.dumps(item)); original=path.read_bytes()
        assert main(['live','on'])==2
        report=json.loads(capsys.readouterr().out)
        assert report['reason'] in ('PROFILE_INVALID','PROFILE_INCOMPLETE')
        assert path.read_bytes()==original
        assert json.loads(index.read_text())['default_profile']=='tokyo'
        capsys.readouterr()
    path.write_text(json.dumps(profile)); original=path.read_bytes()
    path.chmod(0o644)
    assert main(['live','on'])==2; path.chmod(0o600)
    backup=path.with_suffix('.original'); path.rename(backup); path.symlink_to(backup)
    assert main(['live','on'])==2; path.unlink(); backup.rename(path)
    profiles=config_home/'profiles'; moved=config_home/'elsewhere'; profiles.rename(moved); profiles.symlink_to(moved,target_is_directory=True)
    assert main(['live','on'])==2
    profiles.unlink(); moved.rename(profiles)
    assert path.read_bytes()==original
    assert main(['live','profile','show','../../escape'])==2


def test_set_is_atomic_revisioned_and_local(config_home, profile, tmp_path, monkeypatch, capsys):
    remote=tmp_path/'run.env'; remote.write_text('LP_V2_CAPITAL_USDC=100\n')
    original=(config_home/'profiles/tokyo.json').read_bytes()
    monkeypatch.setattr(subprocess,'run',lambda *a,**k: pytest.fail('set must stay local'))
    monkeypatch.setattr('builtins.input',lambda _: 'save')
    assert main(['live','profile','set','tokyo','--capital-usdc','80'])==0
    new=json.loads((config_home/'profiles/tokyo.json').read_text())
    assert new['revision']==2 and new['trading']['capital_usdc']=='80'
    assert new['identity']==profile['identity']
    assert (config_home/'profiles/tokyo.json.bak').read_bytes()==original
    assert (config_home/'profiles/tokyo.json.bak').stat().st_mode & 0o777 == 0o600
    assert remote.read_text()=='LP_V2_CAPITAL_USDC=100\n'
    assert '100' in capsys.readouterr().out


def test_failed_save_preserves_profile_and_default(config_home, profile, monkeypatch, capsys):
    path=config_home/'profiles/tokyo.json'; index=config_home/'config.json'
    original=path.read_bytes(); default=index.read_bytes()
    monkeypatch.setattr('builtins.input',lambda _: 'cancel')
    assert main(['live','profile','set','tokyo','--capital-usdc','80'])==2
    assert path.read_bytes()==original and index.read_bytes()==default
    monkeypatch.setattr('builtins.input',lambda _: 'save')
    replace=os.replace
    def fail_replace(source,target):
        if Path(target)==path:
            raise OSError('synthetic disk failure')
        return replace(source,target)
    with monkeypatch.context() as patch:
        patch.setattr(os,'replace',fail_replace)
        assert main(['live','profile','set','tokyo','--capital-usdc','80'])==2
    assert path.read_bytes()==original and index.read_bytes()==default
    concurrent=copy.deepcopy(profile); concurrent['revision']=2; concurrent['trading']['capital_usdc']='90'
    def race(_):
        path.write_text(json.dumps(concurrent))
        return 'save'
    monkeypatch.setattr('builtins.input',race)
    assert main(['live','profile','set','tokyo','--capital-usdc','80'])==2
    assert json.loads(path.read_text())==concurrent and index.read_bytes()==default
    capsys.readouterr()
    monkeypatch.setattr('builtins.input',lambda _: 'cancel')
    assert main(['live','profile','set-default','tokyo'])==2
    assert index.read_bytes()==default
    # A failed combined profile/index save must roll back its first replacement.
    path.write_bytes(original)
    imported=copy.deepcopy(profile)
    def ssh(argv,**kw):
        if known_ssh(argv): return known_ssh(argv)
        return subprocess.CompletedProcess(argv,0,json.dumps({'profile':imported}),'')
    monkeypatch.setattr(subprocess,'run',ssh)
    monkeypatch.setattr('builtins.input',lambda _: 'save')
    def fail_index(source,target):
        if Path(target)==index:
            raise OSError('index failure')
        return replace(source,target)
    with monkeypatch.context() as patch:
        patch.setattr(os,'replace',fail_index)
        assert main(['live','setup','--profile','tokyo','--ssh-host','synthetic-tokyo','--capital-usdc','80','--set-default'])==2
    assert path.read_bytes()==original and index.read_bytes()==default


def known_ssh(argv):
    if argv[:2]==['ssh','-G']:
        return subprocess.CompletedProcess(argv,0,'hostname synthetic\nport 22\nuserknownhostsfile /synthetic/known_hosts\n','')
    if argv[0]=='ssh-keygen':
        value='synthetic ssh-ed25519 AAAA\n' if '-F' in argv else '256 SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA synthetic (ED25519)\n'
        return subprocess.CompletedProcess(argv,0,value,'')


def test_setup_reports_saved_draft_missing_fields(config_home,profile,monkeypatch,capsys):
    (config_home/'profiles/tokyo.json').unlink();(config_home/'config.json').unlink()
    imported=copy.deepcopy(profile);del imported['files']['relayer_credentials'];del imported['trading']['capital_usdc']
    calls=[]
    def ssh(argv,**kw):
        if known_ssh(argv):return known_ssh(argv)
        request=json.loads(kw['input']);calls.append(request['action'])
        data=dict(profile=imported) if request['action']=='import' else dict(schema_version=1,status='UNKNOWN',checks={})
        return subprocess.CompletedProcess(argv,0,json.dumps(data),'')
    monkeypatch.setattr(subprocess,'run',ssh)
    answers=iter(['100','save']);monkeypatch.setattr('builtins.input',lambda _:next(answers))
    assert main(['live','setup','--profile','tokyo','--ssh-host','synthetic-tokyo','--set-default'])==2
    report=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report['status']=='SAVED_DRAFT' and report['local_saved'] is True
    assert report['name']=='tokyo' and report['revision']==1
    assert report['missing']==['files.relayer_credentials'] and report['can_enable'] is False
    assert report['default_saved'] is False and not (config_home/'config.json').exists()
    path=config_home/'profiles/tokyo.json'
    assert json.loads(path.read_text())['trading']['capital_usdc']=='100'
    assert path.stat().st_mode & 0o777 ==0o600 and path.parent.stat().st_mode & 0o777 ==0o700
    assert calls==['import']
    imported=copy.deepcopy(profile);monkeypatch.setattr('builtins.input',lambda _:'save')
    assert main(['live','setup','--profile','tokyo','--ssh-host','synthetic-tokyo','--set-default'])==0
    completed=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert completed['status']=='SAVED' and completed['missing']==[] and completed['revision']==2
    assert json.loads((config_home/'config.json').read_text())['default_profile']=='tokyo'
    assert calls==['import','import','check']
