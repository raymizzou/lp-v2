from configparser import ConfigParser
from pathlib import Path
import subprocess


def test_live_service_stop_contract():
    path=Path('deploy/lp-v2-live.service')
    assert path.is_file()
    unit=ConfigParser(interpolation=None)
    unit.read(path)
    service=unit['Service']
    assert service['User']==service['Group']=='lp-v2'
    assert 'run-live' in service['ExecStart']
    assert 'lp-v2 stop --state-dir /opt/lp-v2/state' in service['ExecStop']
    assert int(service['TimeoutStopSec'])>=120
    assert service['UMask']=='0077'
    assert service['ReadWritePaths']=='/opt/lp-v2/state'
    launcher=Path('deploy/run-live')
    script=launcher.read_text()
    assert subprocess.run(['bash','-n',str(launcher)],capture_output=True).returncode==0
    for flag in ('--live','--market','--private-key-file','--credentials-file','--relayer-credentials-file','--state-dir','--capital-usdc','--code-sha'):
        assert flag in script
    assert '/etc/lp-v2/signing-key' in script and '/etc/lp-v2/credentials.json' in script
    assert 'systemctl' not in script and 'ssh ' not in script
    assert 'run-readonly' in Path('deploy/lp-v2.service').read_text()
    docs=Path('docs/M4_LIVE.md').read_text()
    for text in ('0600','0700','POLY_PROXY','UNKNOWN','UTC','重新对账','未部署','未开实盘'):
        assert text in docs
    assert 'docs/M4_LIVE.md' in Path('README.md').read_text()
