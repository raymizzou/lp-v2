import io,json,os,subprocess,tarfile
from pathlib import Path
import pytest

SHA='a'*40


@pytest.fixture
def deployment(tmp_path):
    script=Path('deploy/deploy-lp-v2').resolve()
    assert script.is_file()
    root=tmp_path/'lp-v2';root.mkdir();(root/'repo').mkdir()
    binaries=tmp_path/'bin';binaries.mkdir();log=tmp_path/'mutations'
    # External commands are exclusively offline substitutes.
    def fake(name,body):
        p=binaries/name;p.write_text('#!/usr/bin/env python3\n'+body);p.chmod(0o755)
    fake('gh',"import os\nprint(os.environ['FAKE_CI'])\n")
    fake('git',"import sys,os\nif 'config' in sys.argv: print('https://github.com/raymizzou/lp-v2.git')\nelif 'rev-parse' in sys.argv: print(os.environ['FAKE_SHA'])\nelif 'merge-base' in sys.argv: pass\nelse:\n with open(os.environ['MUTATIONS'],'a') as f:f.write('git mutation\\n')\n if 'archive' in sys.argv:sys.stdout.buffer.write(open(os.environ['ARCHIVE'],'rb').read())\n")
    fake('systemctl',"import os\nwith open(os.environ['MUTATIONS'],'a') as f:f.write('restart\\n')\n")
    fake('curl',"import os\nprint(os.environ['FAKE_HEALTH'])\n")
    fake('sleep', 'pass\n')
    # Intercept venv creation; Python validation still uses the real interpreter.
    python=binaries/'python3'
    real_python=Path(__import__('sys').executable)
    python.write_text(f'#!/bin/bash\nif [[ "$1" == "-m" && "$2" == "venv" ]]; then\n mkdir -p "$3/bin"\n printf "#!/bin/sh\\necho install >> \\\"$MUTATIONS\\\"\\n" > "$3/bin/python"\n chmod +x "$3/bin/python"\nelse\n exec "{real_python}" "$@"\nfi\n');python.chmod(0o755)
    archive=tmp_path/'source.tar'
    with tarfile.open(archive,'w') as tf:
        content=b'[project]\nname="lp-v2"\n';info=tarfile.TarInfo('pyproject.toml');info.size=len(content);tf.addfile(info,io.BytesIO(content))
    run=dict(head_sha=SHA,head_branch='main',event='push',status='completed',conclusion='success',id=1,path='.github/workflows/ci.yml',repository=dict(full_name='raymizzou/lp-v2'))
    env={'PATH':str(binaries)+':/usr/bin:/bin','LP_V2_ROOT':str(root),'LP_V2_CAPITAL_USDC':'100','LP_V2_WALLET_TAIL':'a3F9',
         'FAKE_SHA':SHA,'MUTATIONS':str(log),'ARCHIVE':str(archive),'FAKE_HEALTH':json.dumps(dict(status='ok',code_sha=SHA,capital_usdc='100',wallet_tail='a3F9',region_check='ok'))}
    return script, root, log, env, run


def test_deploy_rejects_untrusted_ci_before_mutation(deployment):
    script, root, log, env, run = deployment
    for runs in ([],[dict(run,conclusion='failure')],[dict(run,head_sha='b'*40)],[dict(run,event='pull_request')],[dict(run,head_branch='feature')]):
        result=subprocess.run(['bash',str(script),SHA],env=dict(env,FAKE_CI=json.dumps(dict(workflow_runs=runs))),text=True,capture_output=True,timeout=30)
        assert result.returncode!=0 and not log.exists(),result.stdout+result.stderr
        assert not (root/'current').exists()
    result=subprocess.run(['bash',str(script),SHA],env=dict(env,FAKE_CI=json.dumps(dict(workflow_runs=[run]))),text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert (root/'current').resolve()==root/'releases'/SHA
    assert 'restart' in log.read_text()
    result=subprocess.run(['bash',str(script),SHA],env=dict(env,FAKE_CI=json.dumps(dict(workflow_runs=[run])),FAKE_HEALTH='{}'),text=True,capture_output=True,timeout=30)
    assert result.returncode!=0 and 'rollback' in result.stderr.lower()
    unit=Path('deploy/lp-v2.service').read_text()
    assert '127.0.0.1' in unit and 'UMask=0077' in unit and 'User=lp-v2' in unit
    assert subprocess.run(['bash','-n',str(script)]).returncode==0


def test_deploy_accepts_explicit_blocked_region(deployment):
    script, root, log, env, run = deployment
    health = json.loads(env['FAKE_HEALTH'])
    health['region_check'] = 'blocked'
    result = subprocess.run(
        ['bash', str(script), SHA],
        env=dict(env, FAKE_CI=json.dumps(dict(workflow_runs=[run])),
                 FAKE_HEALTH=json.dumps(health)),
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root/'current').resolve() == root/'releases'/SHA
    assert (root/'current'/'COMPLETE').read_text().strip() == SHA
    assert 'restart' in log.read_text()


@pytest.mark.parametrize('field,value', [
    pytest.param('status', 'error', id='status'),
    pytest.param('code_sha', 'b'*40, id='sha'),
    pytest.param('capital_usdc', '101', id='capital'),
    pytest.param('wallet_tail', 'b4G0', id='wallet'),
    pytest.param('region_check', 'UNKNOWN', id='region-unknown'),
    pytest.param('region_check', None, id='region-missing'),
    pytest.param('region_check', 'invalid', id='region-invalid'),
])
def test_deploy_rejects_invalid_health_identity(deployment, field, value):
    script, root, log, env, run = deployment
    health = json.loads(env['FAKE_HEALTH'])
    health['region_check'] = 'blocked'
    if value is None:
        del health[field]
    else:
        health[field] = value
    result = subprocess.run(
        ['bash', str(script), SHA],
        env=dict(env, FAKE_CI=json.dumps(dict(workflow_runs=[run])),
                 FAKE_HEALTH=json.dumps(health)),
        text=True, capture_output=True, timeout=30)
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'rollback' in result.stderr.lower()
    assert 'Healthy exact requested release' not in result.stdout
