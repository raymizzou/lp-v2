from tests.test_readonly_sdk import clocked_sdk, step_at
"""Observe the real adapter through HTTP boundaries and the real status handler."""
import base64
from datetime import timedelta
from decimal import Decimal
import json

import httpx

from lp_v2.cli import RunConfig
from lp_v2.readonly_runtime import ReadOnlyRunner
from lp_v2.readonly_sdk import L2Credentials, ReadOnlySDK
from lp_v2.status_server import make_handler
from tests.test_readonly_sdk import AT, external_sdk, market_wire
from tests.test_status_server import request


def credentials():
    return L2Credentials('synthetic-key', base64.urlsafe_b64encode(b'synthetic-secret').decode(),
                         'synthetic-pass', '0x' + 'a' * 40, '0x' + 'b' * 40, 2)


def observing_runner(order_results):
    seen = []
    results = iter(order_results)
    def handle(req):
        seen.append((req.method, req.url.path))
        assert req.method == 'GET'
        if req.url.path == '/data/orders':
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return httpx.Response(200, json=result)
        if req.url.path == '/balance-allowance':
            return httpx.Response(200, json={'balance': '182400000', 'allowances': {}})
        if req.url.path == '/api/geoblock':
            return httpx.Response(200, json={'blocked': False})
        return httpx.Response(200, json={'in_play': False})
    public, public_seen = external_sdk([market_wire()])
    sdk = clocked_sdk(public=public, http=httpx.Client(transport=httpx.MockTransport(handle)),
                      credentials=credentials())
    runner = ReadOnlyRunner(RunConfig(Decimal('100'), AT), sdk)
    return runner, sdk, seen, public_seen


def complete(items):
    return {'data': items, 'next_cursor': 'LTE='}


def test_orders_readback_is_periodic_and_observational():
    runner, sdk, seen, public_seen = observing_runner([
        complete([{'id': 'legacy-private-1'}, {'id': 'legacy-private-2'}]), complete([])])
    handler = make_handler(runner.config, runner)
    try:
        assert request(handler)[2]['wallet_orders'] == dict(
            legacy_order_count='UNKNOWN', as_of='UNKNOWN', last_success_at='UNKNOWN')
        proposal = None
        for seconds, count, observed_at in [(0, 2, 0), (10, 2, 0), (29, 2, 0), (30, 0, 30)]:
            step_at(runner, AT + timedelta(seconds=seconds))
            summary = request(handler)[2]
            expected_time = (AT + timedelta(seconds=observed_at)).isoformat()
            assert summary['wallet_orders'] == dict(
                legacy_order_count=count, as_of=expected_time, last_success_at=expected_time)
            assert all(summary['capital'][k] == '0' for k in (
                'inventory_cost', 'open_buys', 'pending_requests', 'hedge_reserve', 'fee_reserve', 'total'))
            assert summary['safety'] == dict(orders_sent=0, cancels_sent=0, since=AT.isoformat())
            current = summary['markets'][0]['proposed']
            assert current['yes'] == {'price': '0.46', 'quantity': '10'}
            assert current['no'] == {'price': '0.51', 'quantity': '10'}
            assert current['pair_cost'] == '0.97' and current['safety_margin'] == '0.03'
            if proposal is None:
                proposal = current
            assert current == proposal
            assert 'legacy-private' not in json.dumps(summary)
            before = list(seen), list(public_seen)
            for _ in range(3):
                assert request(handler)[2] == summary
            assert (seen, public_seen) == before
        assert [path for _, path in seen].count('/data/orders') == 2
        assert [path for _, path in seen].count('/balance-allowance') == 2
        assert all(method == 'GET' for method, _ in seen + public_seen)
    finally:
        sdk.close()


def test_orders_failure_is_unknown_and_recovers():
    failures = [RuntimeError('synthetic-secret legacy-private-1'),
                httpx.ReadTimeout('synthetic-pass'),
                {'data': False, 'next_cursor': 'LTE='},
                complete([True]), complete([{}]), complete([{'id': False}]),
                {'data': []}]
    for failure in failures:
        runner, sdk, _, _ = observing_runner([
            complete([{'id': 'legacy-private-1'}, {'id': 'legacy-private-2'}]),
            failure, complete([])])
        handler = make_handler(runner.config, runner)
        try:
            step_at(runner, AT)
            assert request(handler)[2]['wallet_orders']['legacy_order_count'] == 2
            step_at(runner, AT + timedelta(seconds=30))
            summary = request(handler)[2]
            assert summary['wallet_orders'] == dict(
                legacy_order_count='UNKNOWN', as_of='UNKNOWN', last_success_at=AT.isoformat())
            assert summary['wallet']['available_usdc'] == '182.4'
            assert summary['freshness']['wallet_as_of'] == (AT + timedelta(seconds=30)).isoformat()
            serialized = json.dumps(summary)
            assert all(secret not in serialized for secret in (
                'synthetic', 'legacy-private', credentials().signer_address, credentials().wallet_address))
            assert summary['capital']['total'] == '0' and summary['markets'][0]['proposed']
            step_at(runner, AT + timedelta(seconds=60))
            recovered_at = (AT + timedelta(seconds=60)).isoformat()
            assert request(handler)[2]['wallet_orders'] == dict(
                legacy_order_count=0, as_of=recovered_at, last_success_at=recovered_at)
        finally:
            sdk.close()


def test_orders_pagination_never_publishes_partial_counts():
    page = lambda cursor: {'data': [{'id': 'legacy-private-page'}], 'next_cursor': cursor}
    failures = [
        [page('next'), httpx.ReadTimeout('synthetic-secret')],
        [page('cycle'), page('cycle')],
        [page(str(i)) for i in range(5)],
        [{'data': [], 'next_cursor': True}],
        [{'data': [], 'next_cursor': ''}],
    ]
    for pages in failures:
        runner, sdk, seen, _ = observing_runner([
            complete([{'id': 'good'}]), *pages, complete([])])
        handler = make_handler(runner.config, runner)
        try:
            step_at(runner, AT)
            step_at(runner, AT + timedelta(seconds=30))
            summary = request(handler)[2]
            assert summary['wallet_orders'] == dict(
                legacy_order_count='UNKNOWN', as_of='UNKNOWN', last_success_at=AT.isoformat())
            assert summary['wallet']['available_usdc'] == '182.4'
            assert [p for _, p in seen].count('/data/orders') == 1 + len(pages)
            assert summary['capital']['total'] == '0'
            assert 'legacy-private' not in json.dumps(summary)
            step_at(runner, AT + timedelta(seconds=60))
            assert request(handler)[2]['wallet_orders']['legacy_order_count'] == 0
        finally:
            sdk.close()


def test_orders_without_credentials_remain_unknown():
    public, _ = external_sdk([])
    seen = []
    def handle(req):
        seen.append(req.url.path)
        assert req.url.path == '/api/geoblock'
        return httpx.Response(200, json={'blocked': False})
    sdk = clocked_sdk(public=public, http=httpx.Client(transport=httpx.MockTransport(handle)))
    runner = ReadOnlyRunner(RunConfig(Decimal('100'), AT), sdk)
    try:
        step_at(runner, AT)
        summary = request(make_handler(runner.config, runner))[2]
        assert summary['wallet_orders'] == dict(
            legacy_order_count='UNKNOWN', as_of='UNKNOWN', last_success_at='UNKNOWN')
        assert summary['wallet']['available_usdc'] == 'UNKNOWN'
        assert '/data/orders' not in seen and '/balance-allowance' not in seen
    finally:
        sdk.close()


def test_orders_port_rejects_boolean_negative_and_malformed_counts():
    from tests.test_readonly_runtime import ScriptedReads
    for invalid in (True, -1, 0.0, '0', None):
        class OrdersBoundary(ScriptedReads):
            def read_orders(self, now):
                return dict(legacy_order_count=invalid if now > AT else 2, as_of=now,
                            raw_order='private-order')
        runner = ReadOnlyRunner(RunConfig(Decimal('100'), AT), OrdersBoundary())
        step_at(runner, AT)
        step_at(runner, AT + timedelta(seconds=30))
        summary = request(make_handler(runner.config, runner))[2]
        assert summary['wallet_orders'] == dict(
            legacy_order_count='UNKNOWN', as_of='UNKNOWN', last_success_at=AT.isoformat())
        assert summary['wallet']['available_usdc'] == '182.4'
        assert 'private-order' not in json.dumps(summary)
