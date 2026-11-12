"""Public wait diagnostics; exchange/file boundaries are the only failure doubles."""
import ast
from pathlib import Path


def test_wait_reason_policy_is_complete():
    from lp_v2.wait_policy import wait_policy
    root = Path(__file__).parents[1] / 'src/lp_v2'
    reasons = set()
    for name in ('rotation_live.py', 'rotation.py', 'live_sdk.py', 'live_runtime.py'):
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == '_ReadbackError':
                    reasons.update(a.value for a in node.args if isinstance(a, ast.Constant))
                if isinstance(node.func, ast.Attribute) and node.func.attr == '_transition':
                    reasons.update(a.value for a in node.args[1:2] if isinstance(a, ast.Constant))
                if isinstance(node.func, ast.Name) and node.func.id in ('blocked', 'RotationDecision'):
                    args = node.args[:2] if node.func.id == 'blocked' else node.args[2:]
                    reasons.update(a.value for a in args if isinstance(a, ast.Constant) and isinstance(a.value, str))
                for kw in node.keywords:
                    if kw.arg == 'code' and isinstance(kw.value, ast.Constant):
                        reasons.add(kw.value.value)
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == 'reason' or isinstance(t, ast.Name) and t.id == 'failure_code' for t in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    reasons.add(node.value.value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith(('waiting_', 'historical_', 'unexpected_error:')) or (name.endswith('handoff.py') and node.value.isupper()):
                    reasons.add(node.value)
    # Successful transitions and fact statuses do not represent waiting.
    reasons -= {'best_executable_candidate', 'current_leads_again', 'current_leads_or_ties',
                'initial_market', 'new_on_seed', 'old_market_cleared', 'entry_verified', 'fresh_checks',
                'PASS', 'FAIL', 'UNKNOWN', 'SUCCESS', 'NEW_STATE_ZERO_CONDITIONAL_BALANCES',
                'OWNED_STOPPED_INVENTORY_MATCH', 'FRESH_ZERO_EXECUTION_RISK_CLEARED', 'BUY'}
    reasons -= {'waiting_', 'historical_', 'unexpected_error:'}  # families checked with concrete members below
    reasons.update(('historical_waiting_orders', 'unexpected_error:NovelFailure@order_query'))
    assert reasons
    for reason in sorted(reasons):
        seconds, next_step = wait_policy(reason)
        assert isinstance(seconds, (int, float)) and seconds >= 0, reason
        assert next_step.strip(), reason

from datetime import timedelta
from decimal import Decimal as D
from tests.test_rotation_live import rotation_world, exiting_world, BID
from tests.test_live_sdk import AT, CID, TOKENS


def test_rotation_inventory_numbers_and_deadline(tmp_path):
    rotation, ex, obs, clock = rotation_world(tmp_path)
    rotation.window_end = AT + timedelta(days=1)
    rotation.step(AT)
    ex.quantities['101'] = D('0.125')  # external balance, absent from owned ledger
    clock[0] = AT + timedelta(seconds=10)
    rotation.scanner.scan_once(clock[0])
    began = clock[0]
    report = rotation.step(clock[0])
    assert report['rotation']['phase']=='running' and report['rotation']['current']==BID
    assert report['rotation']['blocker']=='UNKNOWN' and ex.quantities['101']==D('.125')
    signed = len(ex.posts)
    for elapsed in (3590,3600):
        clock[0] = began + timedelta(seconds=elapsed)
        report=rotation.step(clock[0])['rotation']
        assert report['blocker']=='UNKNOWN' and report['current']==BID and report['entered_count']==1
        assert sum(p.token_id in TOKENS for p in ex.posts)==2 and ex.quantities['101']==D('.125')
        assert all(c.kwargs['asset_type']!='CONDITIONAL' for c in ex.sdk.get_balance_allowance.call_args_list)
        assert not rotation.stop.is_set()
    ex.quantities['101'] = D('0')
    clock[0] += timedelta(seconds=10)
    rotation.scanner.scan_once(clock[0])
    assert rotation.step(clock[0])['rotation']['blocker'] == 'UNKNOWN'

import json
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('boundary', ['balance', 'open_orders', 'trade_history', 'order_query',
                                      'merge_query', 'account_check', 'candidate_market', 'startup_proof'])
def test_unexpected_external_read_escalates(tmp_path, monkeypatch, boundary):
    # Each boundary gets a type unknown to production and a message that must never escape.
    failure = type('Novel_' + boundary, (RuntimeError,), {})
    sentinel = 'PRIVATE-CREDENTIAL-DO-NOT-LOG'
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise failure(sentinel)
    rotation, ex, obs, clock = (exiting_world(tmp_path) if boundary in ('candidate_market', 'account_check')
                               else rotation_world(tmp_path))
    rotation.window_end = AT + timedelta(days=1)
    if boundary in ('candidate_market', 'account_check'):
        obs.ranked = (BID,)  # no successful fallback candidate hides the injected failure
        ex.cancel_unknown = False
    else:
        rotation.step(AT)
    if boundary == 'balance':
        ex.sdk.get_balance_allowance.side_effect = fail
    elif boundary == 'open_orders':
        ex.sdk.list_open_orders.side_effect = fail
    elif boundary == 'trade_history':
        ex.sdk.list_account_trades.side_effect = fail
    elif boundary == 'order_query':
        ex.sdk.get_order.side_effect = fail
    elif boundary == 'merge_query':
        from polymarket.transactions import SyncGaslessTransactionHandle
        from unittest.mock import create_autospec
        from polymarket.clients._transport import SyncTransport
        from polymarket._internal.eoa.rpc import SyncJsonRpcClient
        relay = create_autospec(SyncTransport, instance=True, spec_set=True)
        rotation.sdk.rpc = create_autospec(SyncJsonRpcClient, instance=True, spec_set=True)
        rotation.sdk.relayer = relay
        ex.sdk.merge_positions.return_value = SyncGaslessTransactionHandle('relay-secret-id', None, relay, 1, 0)
        ex.fill('order-1', '2', '.40')
        ex.fill('order-2', '2', '.40')
        clock[0] = AT + timedelta(seconds=10)
        rotation.step(clock[0])
        assert rotation.sdk.journal.events('merge_handle')
        relay.get_json.side_effect = fail
    elif boundary == 'account_check':
        monkeypatch.setattr(obs, 'read_account_readiness', fail)
    elif boundary == 'candidate_market':
        read = obs.read_fixed_market
        def candidate(now, cid):
            if cid != CID:
                return fail()
            return read(now, cid)
        monkeypatch.setattr(obs, 'read_fixed_market', candidate)
    elif boundary == 'startup_proof':
        from lp_v2.live_control import profiles
        rotation.control_run_id = 'a' * 32
        proof_path = tmp_path / 'live-control-run.json'
        proof_path.write_text('{}')
        proof_path.chmod(0o600)
        read = profiles.read_json
        monkeypatch.setattr(profiles, 'read_json', lambda path: fail() if Path(path) == proof_path else read(path))
    # Scan uses its already observed candidate snapshot, independent of injected entry reads.
    if boundary == 'candidate_market':
        monkeypatch.setattr(obs, 'read_fixed_market', read)
        rotation.scanner.scan_once(clock[0])
        monkeypatch.setattr(obs, 'read_fixed_market', candidate)
    else:
        rotation.scanner.scan_once(clock[0])
    clock[0] += timedelta(seconds=10)
    began = clock[0]
    signed = len(ex.posts)
    report = rotation.step(clock[0])
    assert calls, boundary
    old_stop = rotation.current.stop.is_set()
    for elapsed in (0, 1, 610):
        if elapsed:
            clock[0] = began + timedelta(seconds=elapsed)
            report = rotation.step(clock[0])
        blocker = report['rotation']['blocker']
        assert blocker['code'].startswith('unexpected_error:' + failure.__name__ + '@')
        assert blocker['code'].endswith('@' + boundary)
        assert blocker['since'] == began.isoformat()
        assert blocker['escalated'] is (elapsed >= 600)
        assert blocker['next_step'] and blocker['deadline']
        assert 'numbers' in blocker and blocker['safe_to_stop'] in (False, 'UNKNOWN')
        assert sentinel not in json.dumps(report)
        assert sentinel not in str(rotation.sdk.journal.events())
        assert len(ex.posts) == signed and rotation.current.condition_id == CID
        assert not rotation.stop.is_set() and rotation.current.stop.is_set() is old_stop
    if boundary == 'order_query':
        assert not rotation.sdk.orders_verified()
    if boundary == 'merge_query':
        assert rotation.current.ledger.snapshot().capital.total >= D('4') * D('.4')
        assert len(rotation.sdk.journal.events('merge_intent')) == 1


def test_round_wait_escalation_and_recovery(tmp_path, monkeypatch):
    from tests.test_live_runtime import world
    from dataclasses import replace
    runner, ex, sdk, journal, obs = world(tmp_path)
    runner.step(AT)
    original = obs.read_region_policy
    class NovelRegionFailure(RuntimeError):
        pass
    def fail():
        raise NovelRegionFailure('PRIVATE-ROUND-SECRET')
    monkeypatch.setattr(obs, 'read_region_policy', fail)
    for seconds, escalated in ((10, False), (610, True)):
        ex.now = AT + timedelta(seconds=seconds)
        blocker = runner.step(ex.now)['blocker']
        assert blocker['code'] == 'unexpected_error:NovelRegionFailure@region'
        assert blocker['since'] == (AT + timedelta(seconds=10)).isoformat()
        assert blocker['deadline'] == (AT + timedelta(seconds=610)).isoformat()
        assert blocker['escalated'] is escalated
        assert blocker['next_step'] and blocker['numbers'] == 'UNKNOWN'
        assert blocker['safe_to_stop'] == 'UNKNOWN'
        assert len(ex.posts) == 2 and not runner.stop.is_set()
        assert 'PRIVATE-ROUND-SECRET' not in str(runner.snapshot()) + str(journal.events())
    assert {c.kwargs['order_id'] for c in ex.sdk.cancel_order.call_args_list} == {'order-1', 'order-2'}
    monkeypatch.setattr(obs, 'read_region_policy', original)
    ex.now += timedelta(seconds=10)
    obs.plans = tuple(replace(p, books=tuple(replace(b, observed_at=ex.now) for b in p.books)) for p in obs.plans)
    assert runner.step(ex.now)['blocker'] == 'UNKNOWN'
    monkeypatch.setattr(obs, 'read_region_policy', fail)
    ex.now += timedelta(seconds=10)
    blocker = runner.step(ex.now)['blocker']
    assert blocker['since'] == ex.now.isoformat() and blocker['escalated'] is False


def test_clearance_keeps_unexpected_balance_decode_type(tmp_path):
    rotation, ex, obs, clock = exiting_world(tmp_path)
    rotation.window_end = AT + timedelta(days=1)
    sentinel = 'DO-NOT-LOG-THIS-MESSAGE'
    decoded = []
    class NovelBalanceDecodeFailure(RuntimeError):
        pass
    class UnreadableBalance:
        @property
        def balance(self):
            decoded.append(True)
            raise NovelBalanceDecodeFailure(sentinel)
    original = ex.balance
    def read(**kw):
        return UnreadableBalance() if kw['asset_type'] == 'CONDITIONAL' else original(**kw)
    ex.sdk.get_balance_allowance.side_effect = read
    clock[0] += timedelta(seconds=10)
    began = clock[0]
    posts = len(ex.posts)
    waiting_since=AT+timedelta(seconds=10)
    for elapsed, escalated in ((0,False),(1810,True)):
        clock[0] = began + timedelta(seconds=elapsed)
        rotation.step(clock[0])
        report = rotation.snapshot()
        assert decoded==[]
        blocker = report['rotation']['blocker']
        assert blocker['code']=='orders_remaining'
        assert blocker['since']==waiting_since.isoformat()
        assert blocker['deadline']==(waiting_since+timedelta(seconds=1800)).isoformat()
        assert blocker['escalated'] is escalated
        assert blocker['next_step']
        assert len(ex.posts) == posts
        assert report['rotation']['current'] == CID and report['rotation']['phase'] == 'exiting'
        assert report['rotation']['entered_count'] == 0
        assert not rotation.stop.is_set() and rotation.current.stop.is_set()
        assert sentinel not in str(report) + str(rotation.sdk.journal.events()) + str(rotation.journal.events())


def test_exiting_clearance_diagnostic_overrides_missing_startup_proof(tmp_path, monkeypatch):
    from dataclasses import replace
    from lp_v2.live_control import profiles
    rotation, ex, obs, clock = exiting_world(tmp_path)
    identity = dict(wallet_address=rotation.sdk.wallet, signer_address=rotation.sdk.signer, signature_type=1)
    credentials = dict(identity, key='synthetic', secret='synthetic', passphrase='synthetic')
    l2 = tmp_path / 'l2.json'
    profiles.atomic_json(l2, credentials)
    rotation.config = replace(rotation.config, credentials=SimpleNamespace(**credentials),
        private_key_file=str(tmp_path / 'signing'), relayer_credentials_file=str(tmp_path / 'relayer'))
    rotation.action_id, rotation.control_run_id = 'a' * 32, 'b' * 32
    profile = dict(identity=identity, trading=dict(market_condition_id=CID, capital_usdc='100'),
        release=dict(expected_sha=rotation.config.code_sha), files=dict(state_dir=str(tmp_path),
        signing_key=rotation.config.private_key_file, relayer_credentials=rotation.config.relayer_credentials_file,
        l2_credentials=str(l2)))
    record = dict(profile=profile, action_id=rotation.action_id, run_id=rotation.control_run_id,
        started_at=AT.isoformat(), window_end=rotation.window_end.isoformat())
    proof_path = tmp_path / 'live-control-run.json'
    profiles.atomic_json(proof_path, record)  # bound on/run deliberately has no startup_verified
    profiles.atomic_json(tmp_path / 'process.json', dict(run_id=rotation.control_run_id))
    reads = []
    read_json = profiles.read_json
    def observed_read(path):
        result = read_json(path)
        reads.append(Path(path))
        return result
    monkeypatch.setattr(profiles, 'read_json', observed_read)
    ex.fill('order-1', '1', '.40', at=clock[0] + timedelta(seconds=1))
    openings = {r.request_id for r in rotation.current.ledger.snapshot().requests if r.purpose == 'opening'}
    signed_openings = [p for _, p in rotation.sdk.journal.events('signed') if p['request_id'] in openings]
    clock[0] += timedelta(seconds=10)
    report = rotation.step(clock[0])['rotation']
    # Repeated validated reads prove the bound-window validation completed before the missing-proof read.
    assert reads.count(proof_path) >= 2 and 'startup_verified' not in read_json(proof_path)
    assert report['phase'] == 'exiting' and report['current'] == CID
    assert report['reason']=='waiting_orders'
    assert report['blocker']['code']=='orders_remaining'
    assert report['blocker']['numbers']=='UNKNOWN'
    assert {r.request_id for r in rotation.current.ledger.snapshot().requests if r.purpose == 'opening'} == openings
    assert [p for _, p in rotation.sdk.journal.events('signed') if p['request_id'] in openings] == signed_openings
    assert report['entered_count'] == 0
    assert not rotation.stop.is_set() and rotation.current.stop.is_set()


def test_alternating_wait_codes_escalate_without_recovery(tmp_path):
    rotation, ex, obs, clock = rotation_world(tmp_path)
    rotation.window_end = AT + timedelta(days=1)
    rotation.step(AT)
    ex.quantities['101'] = D('0.125')
    ex.cancel_unknown=True
    began = AT + timedelta(seconds=10)
    class Flaky(RuntimeError):
        pass
    original = ex.balance
    def balance(**kw):
        if kw['asset_type'] == 'COLLATERAL' and int((clock[0] - began).total_seconds()) // 10 % 2:
            raise Flaky('PRIVATE-FLAKY-READ')
        return original(**kw)
    ex.sdk.get_balance_allowance.side_effect = balance
    rotation.scanner.scan_once(AT)
    posts = len(ex.posts)
    codes = set()
    for elapsed in range(0, 4000, 10):
        clock[0] = began + timedelta(seconds=elapsed)
        report = rotation.step(clock[0])['rotation']
        blocker = report['blocker']
        expected = 'unexpected_error:Flaky@balance' if elapsed // 10 % 2 else 'orders_remaining'
        assert blocker['code'] == expected
        codes.add(blocker['code'])
        assert blocker['since'] == clock[0].isoformat()  # the per-code clock still resets
        assert report['phase'] == 'exiting' and report['current'] == CID
        assert len(ex.posts) == posts and report['entered_count'] == 0
        assert not rotation.stop.is_set() and rotation.current.stop.is_set()
        assert 'PRIVATE-FLAKY-READ' not in str(rotation.snapshot()) + str(rotation.sdk.journal.events())
        if expected=='orders_remaining':
            assert blocker['numbers']=='UNKNOWN' and blocker['safe_to_stop'] is False
            assert blocker['order_tail'] in ('order-1','order-2')
        if elapsed >= 3600:
            assert blocker['escalated'] is True
            assert blocker['episode_since'] == began.isoformat()
            assert blocker['deadline'] <= (began + timedelta(seconds=3600)).isoformat()
    assert codes == {'orders_remaining','unexpected_error:Flaky@balance'}

    # Real exchange recovery and a current-leading scan resume running with no blocker.
    ex.sdk.get_balance_allowance.side_effect = original
    ex.quantities['101'] = D('0')
    obs.ranked = (CID,)
    clock[0] += timedelta(seconds=10)
    rotation.scanner.scan_once(clock[0])
    recovered = rotation.step(clock[0])['rotation']
    assert recovered['phase'] == 'running' and recovered['blocker'] == 'UNKNOWN'
    assert recovered['entered_count'] == 0
    # A fresh wait after observed recovery has a fresh episode and is not escalated.
    ex.quantities['101'] = D('0.125')
    obs.ranked = (CID, BID)
    clock[0] += timedelta(seconds=10)
    rotation.scanner.scan_once(clock[0])
    fresh = rotation.step(clock[0])['rotation']
    assert fresh['phase'] == 'exiting' and fresh['blocker']['code'] == 'orders_remaining'
    assert fresh['blocker']['episode_since'] == clock[0].isoformat()
    assert fresh['blocker']['escalated'] is False
    # Without an intervening empty blocker, a real exit to cleared starts another episode.
    ex.quantities['101'] = D('0')
    ex.cancel_unknown=False
    obs.fail_public.update((CID, BID))
    clock[0] = rotation.window_end + timedelta(seconds=10)
    changed = rotation.step(clock[0])['rotation']
    assert changed['phase'] == 'cleared' and changed['reason'] == 'no_revalidated_candidate'
    assert changed['blocker']['episode_since'] == clock[0].isoformat()
    assert changed['blocker']['since'] == clock[0].isoformat()
    assert changed['entered_count'] == 0 and not rotation.stop.is_set()
