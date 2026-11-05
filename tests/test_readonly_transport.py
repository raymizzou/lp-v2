from tests.test_readonly_sdk import clocked_sdk, read_at, step_at
"""Audit dispatch at real httpx transports, without socket networking."""
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
import json

import httpx
import pytest
from polymarket import PublicClient

from lp_v2.cli import RunConfig
from lp_v2.readonly_runtime import ReadOnlyRunner
from lp_v2.readonly_sdk import ReadOnlySDK
from lp_v2.status_server import make_handler
from tests.test_readonly_observation import credentials, complete
from tests.test_readonly_sdk import AT, CID, market_wire
from tests.test_status_server import request


def wire_responder(seen):
    def handle(req):
        seen.append(req)
        if req.url.path == '/balance-allowance':
            payload = {'balance': '182400000', 'allowances': {}}
        elif req.url.path == '/data/orders':
            payload = complete([{'id': 'private-order'}])
        elif req.url.path == '/api/geoblock':
            payload = {'blocked': False}
        elif req.url.path == '/markets/keyset':
            payload = ({'markets': [], 'next_cursor': None} if req.url.params.get('after_cursor')
                       else {'markets': [market_wire()], 'next_cursor': 'page2'})
        elif req.url.path == '/book':
            token = req.url.params['token_id']
            bid, ask = ('0.46', '0.48') if token == '101' else ('0.51', '0.53')
            payload = dict(market=CID, asset_id=token, timestamp='1793577600000',
                           bids=[dict(price=bid, size='100')], asks=[dict(price=ask, size='100')],
                           min_order_size='1', tick_size='0.01', neg_risk=False, hash='fixture')
        elif req.url.path == '/markets/' + CID:
            payload = {'in_play': False}
        else:
            raise AssertionError('unexpected external route')
        return httpx.Response(200, json=payload)
    return handle


def install_external_transports(monkeypatch, seen):
    """Patch only httpx construction; SDK and PublicClient wiring remain real.

    The additional mount exercises httpx's routed outbound boundary as well as
    its default transport. Retain clients to test callers holding those handles.
    """
    clients = []
    original = httpx.Client.__init__
    def initialize(client, *args, **kwargs):
        kwargs.update(transport=httpx.MockTransport(wire_responder(seen)), trust_env=False,
                      mounts={'https://clob.polymarket.com': httpx.MockTransport(wire_responder(seen))})
        original(client, *args, **kwargs)
        clients.append(client)
    monkeypatch.setattr(httpx.Client, '__init__', initialize)
    return clients


def test_transport_counts_account_and_public_gets(monkeypatch):
    for injected in (False, True):
        seen = []
        with monkeypatch.context() as patch:
            clients = install_external_transports(patch, seen)
            public = PublicClient() if injected else None
            http = httpx.Client() if injected else None
            before = datetime.now(UTC)
            sdk = clocked_sdk(public=public, http=http, credentials=credentials())
            after = datetime.now(UTC)
            try:
                initial = sdk.transport_safety()
                assert initial['get_attempts'] == initial['rejected_non_get_attempts'] == 0
                assert initial['status'] == 'ok'
                assert before <= initial['monitor_started_at'] <= after
                assert len(clients) == 5
                assert sdk.read_wallet(AT)['available_usdc'] == '182.4'
                assert sdk.read_orders(AT)['legacy_order_count'] == 1
                assert sdk.read_region() == 'ok'
                assert read_at(sdk, 'read_markets', AT)[0]['plan'] is not None
                assert Counter(r.url.path for r in seen) == {
                    '/balance-allowance': 1, '/data/orders': 1, '/api/geoblock': 1,
                    '/markets/keyset': 2, '/markets/' + CID: 1, '/book': 2}
                assert len(seen) == sdk.transport_safety()['get_attempts'] == 8
                sdk.read_wallet(AT)
                sdk.read_region()
                assert len(seen) == sdk.transport_safety()['get_attempts'] == 10
                assert all(r.method == 'GET' for r in seen)
                assert sdk.safety(AT) == dict(orders_sent=0, cancels_sent=0, since=AT)
                runner = ReadOnlyRunner(RunConfig(Decimal('100'), AT), sdk)
                step_at(runner, AT)
                summary = request(make_handler(runner.config, runner))[2]
                assert summary['transport_safety'] == dict(
                    get_attempts=18, rejected_non_get_attempts=0, status='ok',
                    monitor_started_at=initial['monitor_started_at'].isoformat())
                assert len(seen) == 18
                assert summary['safety'] == dict(orders_sent=0, cancels_sent=0, since=AT.isoformat())
                serialized = json.dumps(summary)
                assert all(s not in serialized for s in (
                    'synthetic', 'private-order', credentials().signer_address, credentials().wallet_address))
            finally:
                sdk.close()
                for client in clients:
                    client.close()


def test_transport_rejects_non_get_and_preserves_violation(monkeypatch):
    seen = []
    clients = install_external_transports(monkeypatch, seen)
    public, http = PublicClient(), httpx.Client()
    sdk = clocked_sdk(public=public, http=http, credentials=credentials())
    other = clocked_sdk(credentials=credentials())
    guarded_clients = clients[:5]
    try:
        attempts = 0
        for client in guarded_clients:
            for url in ('https://gamma.polymarket.com/data/orders?secret=synthetic-secret',
                        'https://clob.polymarket.com/data/orders?secret=synthetic-secret'):
                for method in ('POST', 'DELETE', 'PUT', 'PATCH', 'HEAD', 'OPTIONS', 'BREW'):
                    with pytest.raises(RuntimeError) as rejected:
                        client.request(method, url, headers={'X-Synthetic': 'synthetic-pass'})
                    assert str(rejected.value) == 'read-only transport rejected non-GET'
                    attempts += 1
                    evidence = sdk.transport_safety()
                    assert evidence['rejected_non_get_attempts'] == attempts
                    assert evidence['get_attempts'] == 0 and evidence['status'] == 'violation'
                    assert seen == []
        # Documented public batch-book API uses POST, even though it is a read.
        with pytest.raises(RuntimeError, match='^read-only transport rejected non-GET$'):
            public.get_order_books(token_ids=['101', '102'])
        attempts += 1
        assert attempts == 71 and seen == []
        # A downstream read may swallow the error; the violation must survive.
        def change_method(req):
            req.method = 'POST'
        http.event_hooks['request'].append(change_method)
        assert sdk.read_region() == 'UNKNOWN'
        attempts += 1
        assert attempts == 72 and seen == []
        http.event_hooks['request'].clear()
        assert sdk.read_region() == 'ok'
        assert sdk.transport_safety()['status'] == 'violation'
        assert sdk.transport_safety()['get_attempts'] == len(seen) == 1
        assert sdk.safety(AT) == dict(orders_sent=0, cancels_sent=0, since=AT)
        assert other.transport_safety()['status'] == 'ok'
        assert other.transport_safety()['get_attempts'] == other.transport_safety()['rejected_non_get_attempts'] == 0
        runner = ReadOnlyRunner(RunConfig(Decimal('100'), AT), sdk)
        step_at(runner, AT)
        summary = request(make_handler(runner.config, runner))[2]
        assert summary['transport_safety']['status'] == 'violation'
        assert summary['transport_safety']['rejected_non_get_attempts'] == 72
        assert summary['transport_safety']['get_attempts'] == len(seen) == 9
        assert summary['safety'] == dict(orders_sent=0, cancels_sent=0, since=AT.isoformat())
        assert all(r.method == 'GET' for r in seen)
        assert 'synthetic' not in json.dumps(summary) and 'secret=' not in json.dumps(summary)
        assert other.transport_safety()['get_attempts'] == other.transport_safety()['rejected_non_get_attempts'] == 0
    finally:
        sdk.close()
        other.close()
        for client in clients:
            client.close()


def test_missing_transport_instrumentation_is_unknown_and_disables_reads(monkeypatch):
    seen = []
    clients = install_external_transports(monkeypatch, seen)
    public, http = PublicClient(), httpx.Client()
    # Simulate a future SDK shape that no longer exposes an auditable client.
    data_client = public._ctx.data._client
    public._ctx.data._client = object()
    sdk = clocked_sdk(public=public, http=http, credentials=credentials())
    try:
        assert sdk.transport_safety() == dict(
            get_attempts='UNKNOWN', rejected_non_get_attempts='UNKNOWN',
            status='UNKNOWN', monitor_started_at='UNKNOWN')
        assert sdk.safety(AT) == dict(orders_sent='UNKNOWN', cancels_sent='UNKNOWN', since=AT)
        assert sdk.read_wallet(AT) == dict(available_usdc='UNKNOWN', as_of=None)
        assert sdk.read_orders(AT) == dict(legacy_order_count='UNKNOWN', as_of=None)
        assert sdk.read_region() == 'UNKNOWN'
        with pytest.raises(RuntimeError, match='^read-only transport audit unavailable$'):
            read_at(sdk, 'read_markets', AT)
        runner = ReadOnlyRunner(RunConfig(Decimal('100'), AT), sdk)
        summary = step_at(runner, AT)
        assert summary['transport_safety']['status'] == 'UNKNOWN'
        assert summary['safety']['orders_sent'] == summary['safety']['cancels_sent'] == 'UNKNOWN'
        assert summary['markets'] == [] and seen == []
    finally:
        public._ctx.data._client = data_client
        sdk.close()
        for client in clients:
            client.close()


def test_failed_get_attempt_is_still_measured(monkeypatch):
    seen = []
    clients = install_external_transports(monkeypatch, seen)
    http = httpx.Client()
    def timeout(req):
        seen.append(req)
        raise httpx.ReadTimeout('synthetic-secret')
    http._transport = httpx.MockTransport(timeout)
    sdk = clocked_sdk(http=http, credentials=credentials())
    try:
        assert sdk.read_region() == 'UNKNOWN'
        assert len(seen) == sdk.transport_safety()['get_attempts'] == 1
        assert sdk.transport_safety()['status'] == 'ok'
        assert sdk.safety(AT)['orders_sent'] == sdk.safety(AT)['cancels_sent'] == 0
    finally:
        sdk.close()
        for client in clients:
            client.close()
