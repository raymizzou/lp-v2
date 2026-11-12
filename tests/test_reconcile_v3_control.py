"""Approved v3 control behavior with fake process and HTTP boundaries."""
from datetime import UTC, datetime
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import subprocess
import json
import pytest

from tests.test_live_control_profiles import profile, config_home
from tests.test_live_control_host import target, operator_os, call_host, combined_world


def test_off_uses_process_and_exit_receipt_without_accounting_proof(target, monkeypatch):
    from tests.test_live_control import seed_run, off_boundary
    from tests.test_live_sdk import request
    from lp_v2.journal import Journal
    from lp_v2.models import MergeRequest, MergeResult, Outcome
    owner = 'lp-v2:' + target['identity']['wallet_address'].lower()
    cid = target['trading']['market_condition_id']
    at = datetime.now(UTC)
    pending = replace(request(), owner=owner, quote=replace(request().quote, condition_id=cid))
    merge = MergeResult(MergeRequest('unknown-merge', owner, cid, '101', '102', D('1'), at),
                        Outcome.UNKNOWN, D('0'), at)
    seed_run(target)
    calls = off_boundary(target, monkeypatch, merge_evidence=(merge,))
    command = subprocess.run
    path = Path(target['files']['state_dir']) / 'journal.sqlite'
    def stop(argv, **kwargs):
        result = command(argv, **kwargs)
        if argv[:3] == ['systemctl', 'stop', 'lp-v2-live.service']:
            journal = Journal(path)
            try:
                previous = journal.events('stop_result')[-1][1]
                journal.append('stop_result', dict(previous, pending=(pending,),
                    reconciliation='UNKNOWN', accounting_as_of=None))
            finally:
                journal.close()
        return result
    monkeypatch.setattr(subprocess, 'run', stop)
    code, report = call_host(dict(schema_version=1, action='off', profile=target,
        run_id='f' * 32, action_id='c' * 32))
    assert report['executor_stopped'] is True and report['port_released'] is True
    assert report['readonly_restored'] is True
    assert report['merge_history']['status'] == 'UNKNOWN'
    assert report['inventory'][0]['quantity'] == '3'
    journal = Journal(path)
    try:
        stopped = journal.events('stop_result')[-1][1]
        assert stopped['reconciliation'] == 'UNKNOWN' and stopped['pending'] == (pending,)
        assert stopped['merges'] == (merge,) and stopped['merges'][0].received_usdc == 0
    finally:
        journal.close()
    assert calls.count(['systemctl', 'stop', 'lp-v2-live.service']) == 1
    assert not any(any(arg in argv for arg in ('merge', 'submit', 'cancel', 'sell')) for argv in calls)
    assert code == 0 and report['status'] == 'PASS' and report['drain'] == 'PASS'


@pytest.mark.parametrize('old_end', [None, '2000-01-01T00:00:00+00:00'])
def test_on_without_future_window_starts_until_off(combined_world, monkeypatch, capsys, old_end):
    from lp_v2.cli import main
    world = combined_world
    confirmations = []
    def confirm(prompt):
        confirmations.append(prompt)
        return 'on'
    monkeypatch.setattr('builtins.input', confirm)
    args = ['live', 'on'] + ([] if old_end is None else ['--window-end', old_end])
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report['status'] == 'PASS' and report['verified_rounds'] == 2
    assert len(confirmations) == 1 and world.lease is not None and not world.readonly
    assert world.mutations == [('stop', 'lp-v2.service'), ('start', 'lp-v2-live.service')]
    assert main(['live', 'off']) == 0
    stopped = json.loads(capsys.readouterr().out)
    assert stopped['drain'] == 'PASS' and stopped['readonly_restored'] is True
    assert world.lease is None and world.readonly


@pytest.mark.parametrize('conditional_status', [200, 503])
def test_preflight_ignores_residual_inventory(combined_world, monkeypatch, capsys, conditional_status):
    from lp_v2.cli import main
    world = combined_world
    world.conditional = {'101': '8133', '102': '0'}
    world.conditional_status = conditional_status
    code = main(['live', 'check'])
    report = json.loads(capsys.readouterr().out)
    assert 'inventory_handoff' not in report['checks']
    assert code == 0 and report['checks']['balance']['status'] == 'PASS'
    assert world.mutations == []
    monkeypatch.setattr('builtins.input', lambda _: 'on')
    assert main(['live', 'on']) == 0
    started = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert started['verified_rounds'] == 2
    assert not any(req.url.params.get('asset_type') == 'CONDITIONAL' for req in world.http_requests)
    assert world.conditional == {'101': '8133', '102': '0'}
    assert main(['live', 'off']) == 0
    capsys.readouterr()


@pytest.mark.parametrize('layout', ['legacy', 'market'])
def test_readonly_control_includes_committed_wal(combined_world, monkeypatch, capsys, layout):
    import sqlite3
    from lp_v2.cli import main
    from lp_v2.journal import Journal
    from lp_v2.rotation_state import market_path, read_market_identity
    from lp_v2.live_control.host import journal_rows
    from tests.test_live_control import seed_run, off_boundary
    world = combined_world
    import tests.test_live_control as control_fakes
    monkeypatch.setattr(control_fakes, 'datetime', type(world.clock))
    p = world.profile
    state = Path(p['files']['state_dir'])
    seed_run(p)
    cid = p['trading']['market_condition_id']
    identity = dict(owner='lp-v2:' + p['identity']['wallet_address'].lower(),
                    condition_id=cid, token_ids=('101', '102'))
    path = state / 'journal.sqlite' if layout == 'legacy' else market_path(state, cid, create=True)
    writer = Journal(path)
    writer.append('identity', identity)
    path.chmod(0o600)
    reader = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    reader.execute('BEGIN')
    reader.execute('SELECT count(*) FROM events').fetchone()
    writer.append('diagnostic', dict(reason='committed-in-wal', value=42))
    writer.append('stop_result', dict(at=world.clock, exit_code=0, orders=[], pending=[],
        positions=[], merges=[], inventory=[], reconciliation='UNKNOWN', accounting_as_of=None))
    writer.close()
    wal = path.with_name(path.name + '-wal')
    for sidecar in (wal, path.with_name(path.name + '-shm')):
        sidecar.chmod(0o600)
    final = state / 'stop-result.json'
    final.write_text(json.dumps(dict(run_id='f' * 32, exit_code=0)))
    final.chmod(0o600)
    before = {f: f.read_bytes() for f in (path, wal)}
    assert before[wal]
    try:
        assert journal_rows(p, 'diagnostic')[-1][1] == dict(reason='committed-in-wal', value=42)
        if layout == 'market':
            assert read_market_identity(state, cid, immutable=True) == identity
        assert main(['live', 'check']) == 0
        assert json.loads(capsys.readouterr().out)['checks']['state_identity']['status'] == 'PASS'
        assert {f: f.read_bytes() for f in before} == before
        off_boundary(p, monkeypatch)
        command = subprocess.run
        def stopped(argv, **kwargs):
            if argv[:3] == ['systemctl', 'stop', 'lp-v2-live.service']:
                return subprocess.CompletedProcess(argv, 0, '', '')
            return command(argv, **kwargs)
        monkeypatch.setattr(subprocess, 'run', stopped)
        code, report = call_host(dict(schema_version=1, action='off', profile=p,
            run_id='f' * 32, action_id='c' * 32))
        assert code == 0 and report['drain'] == 'PASS' and report['readonly_restored'] is True, report
        assert {f: f.read_bytes() for f in before} == before
    finally:
        reader.close()


@pytest.mark.parametrize('accounting', ['SUCCESS', 'UNKNOWN'])
def test_fresh_paused_start_does_not_rollback(combined_world, monkeypatch, capsys, accounting):
    import httpx
    from lp_v2.cli import main
    world = combined_world
    world.pause = 'protective'
    response = httpx.HTTPTransport.handle_request
    def observed(transport, request):
        result = response(transport, request)
        if request.url.path == '/summary' and world.lease:
            row = result.json()
            row['accounting']['status'] = accounting
            return httpx.Response(200, json=row, request=request)
        return result
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', observed)
    monkeypatch.setattr('builtins.input', lambda _: 'on')
    assert main(['live', 'on']) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report['verified_rounds'] == 2 and report['reason'] == 'INELIGIBLE'
    assert 'rollback' not in report and world.lease is not None
    assert world.mutations == [('stop', 'lp-v2.service'), ('start', 'lp-v2-live.service')]
    assert main(['live', 'off']) == 0
    capsys.readouterr()


@pytest.mark.parametrize('bad', ['tokens', 'owner'])
def test_prepare_retains_journal_identity_guard(combined_world, bad):
    from lp_v2.journal import Journal
    from lp_v2.live_control.profiles import fingerprint
    from tests.test_live_control_host import seal_fixture_journals
    world = combined_world
    profile = world.profile
    state = Path(profile['files']['state_dir'])
    owner = 'lp-v2:' + profile['identity']['wallet_address'].lower()
    journal = Journal(state / 'journal.sqlite')
    try:
        journal.append('identity', dict(owner='foreign' if bad == 'owner' else owner,
            condition_id=profile['trading']['market_condition_id'],
            token_ids=('999', '102') if bad == 'tokens' else ('101', '102')))
    finally:
        journal.close()
    (state / 'journal.sqlite').chmod(0o600)
    seal_fixture_journals(state)
    env = Path(profile['files']['run_env'])
    env.write_bytes(env.read_bytes().replace(b'LP_V2_CAPITAL_USDC=100', b'LP_V2_CAPITAL_USDC=80'))
    before = env.read_bytes()
    journal_before = (state / 'journal.sqlite').read_bytes()
    request = dict(schema_version=1, action='prepare', profile=profile, action_id='b' * 32,
        changes=dict(LP_V2_MARKET=profile['trading']['market_condition_id'],
            LP_V2_CAPITAL_USDC='100', LP_V2_CODE_SHA=profile['release']['expected_sha']),
        confirmation=dict(operation='prepare', profile_sha256=fingerprint(profile),
            issued_at=world.clock.isoformat()))
    code, raw = world.invoke(request)
    report = json.loads(raw)
    assert code == 2 and report['checks']['state_identity']['status'] != 'PASS'
    assert env.read_bytes() == before and not env.with_name(env.name + '.bak').exists()
    assert (state / 'journal.sqlite').read_bytes() == journal_before
    assert world.mutations == [] and world.readonly is True
