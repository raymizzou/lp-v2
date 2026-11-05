"""SSH identity and lost-response behavior through the public CLI."""
import copy
import json
import subprocess
import pytest
from lp_v2.cli import main
from tests.test_live_control_profiles import profile, config_home


def test_ssh_rejects_wrong_identity_and_command_injection(config_home, profile, monkeypatch, capsys):
    calls=[]
    def boundary(argv,**kw):
        calls.append(argv)
        if argv[:2]==['ssh','-G']:
            return subprocess.CompletedProcess(argv,0,'hostname synthetic\nport 22\nuserknownhostsfile /synthetic/known_hosts\n','')
        if argv[0]=='ssh-keygen' and '-F' in argv:
            return subprocess.CompletedProcess(argv,0,'synthetic ssh-ed25519 AAAA\n','')
        if argv[0]=='ssh-keygen':
            return subprocess.CompletedProcess(argv,0,'256 SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB synthetic (ED25519)\n','')
        pytest.fail('wrong fingerprint reached target')
    monkeypatch.setattr(subprocess,'run',boundary)
    assert main(['live','check'])==2
    assert json.loads(capsys.readouterr().out)['reason']=='SSH_IDENTITY_MISMATCH'
    assert all(argv[0]=='ssh-keygen' or argv[:2]==['ssh','-G'] for argv in calls)
    for section,field,evil in [('target','ssh_host','host;touch /tmp/injected'),('release','root','/opt/$(id)'),('files','state_dir','/opt/state;id')]:
        p=copy.deepcopy(profile); p[section][field]=evil
        (config_home/'profiles/tokyo.json').write_text(json.dumps(p))
        calls.clear(); assert main(['live','check'])==2; assert not calls
        capsys.readouterr()


def test_response_loss_never_replays_action(config_home, profile, monkeypatch, capsys):
    from tests.test_live_control_profiles import known_ssh
    calls=[]
    def boundary(argv,**kw):
        if known_ssh(argv): return known_ssh(argv)
        request=json.loads(kw['input']); calls.append(request['action'])
        if request['action']=='on':
            raise subprocess.TimeoutExpired(argv,180)
        from tests.test_live_control import ready_report
        result=ready_report(profile) if request['action']=='check' else {'status':'UNKNOWN','reason':'ACTION_PENDING'}
        return subprocess.CompletedProcess(argv,0,json.dumps(result),'')
    monkeypatch.setattr(subprocess,'run',boundary)
    monkeypatch.setattr('builtins.input',lambda prompt: 'on')
    args=['live','on','--action-id','a'*32,'--window-end','2099-01-01T00:00:00+00:00']
    assert main(args)==2
    first=capsys.readouterr().out
    assert main(args)==2
    second=capsys.readouterr().out
    assert calls.count('on')==1 and calls[-1]=='result'
    assert 'UNKNOWN' in first and 'UNKNOWN' in second
    record=config_home/'operations'/('a'*32+'.json')
    assert record.exists() and record.stat().st_mode & 0o777 == 0o600
    assert json.loads(record.read_text())['action']=='on'
