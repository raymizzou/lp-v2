from email.parser import BytesParser
from dataclasses import FrozenInstanceError
from io import BytesIO
import json
from decimal import Decimal

import pytest

from lp_v2.cli import RunConfig, parse_run
from lp_v2.models import Balance
from tests.fakes.fake_sdk import FakeSDK
from tests.test_ledger import AT, T1, place_pair, prepared_ledger


class MemoryConnection:
    """Only the transport boundary is replaced; the HTTP parser/handler are real."""
    def __init__(self, request):
        self.input = BytesIO(request)
        self.output = BytesIO()

    def makefile(self, mode, buffering=-1):
        assert mode == "rb"
        return self.input

    def sendall(self, data):
        self.output.write(data)


def request(handler, method="GET", path="/summary", payload=b""):
    wire = (f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload
    connection = MemoryConnection(wire)
    handler(connection, ("127.0.0.1", 12345), None)
    headers, body = connection.output.getvalue().split(b"\r\n\r\n", 1)
    status, fields = headers.split(b"\r\n", 1)
    headers = BytesParser().parsebytes(fields)
    decoded = json.loads(body) if body and headers.get_content_type() == "application/json" else body or None
    return int(status.split()[1]), headers, decoded



def original_fields(payload):
    return {key: ({k: v for k, v in payload[key].items() if k != 'proposed_reserve'}
                  if key == 'capital' else payload[key])
            for key in ('version', 'capital_usdc', 'started_at', 'capital')}

def test_get_status_json():
    from lp_v2.status_server import make_handler

    config = RunConfig(Decimal("100"), AT)
    ledger, requests = prepared_ledger()
    handler = make_handler(config, ledger)
    before = ledger.snapshot()
    status, headers, body = request(handler, path="/healthz")
    assert status == 200 and headers.get_content_type() == "application/json"
    assert body == {"version": "0.1.0", "status": "ok", "code_sha":"UNKNOWN", "capital_usdc":"100", "wallet_tail":"UNKNOWN", "region_check":"UNKNOWN"}
    status, headers, body = request(handler)
    assert status == 200 and headers.get_content_type() == "application/json"
    assert original_fields(body) == {
        "version": "0.1.0", "capital_usdc": "100", "started_at": "2026-11-02T00:00:00+00:00",
        "capital": {"inventory_cost": "0", "open_buys": "0", "pending_requests": "9.50",
                    "hedge_reserve": "0.30", "fee_reserve": "0.20", "total": "10.00"},
    }
    assert ledger.snapshot() == before

    fake = FakeSDK(balance=Balance(Decimal("100"), AT))
    yes, no = place_pair(fake, requests)
    for result in (yes, no):
        ledger.apply_order(result)
    ledger.apply_fill(fake.execute_fill(yes.order.order_id, trade_id="status-four", quantity=Decimal("4"),
                                       price=Decimal("0.40"), fee_usdc=Decimal("0.04"), at=T1))
    before = ledger.snapshot()
    status, headers, current = request(handler)
    assert status == 200 and headers.get_content_type() == "application/json"
    assert original_fields(current) == {
        "version": "0.1.0", "capital_usdc": "100", "started_at": "2026-11-02T00:00:00+00:00",
        "capital": {"inventory_cost": "1.64", "open_buys": "7.90", "pending_requests": "0",
                    "hedge_reserve": "0.30", "fee_reserve": "0.16", "total": "10.00"},
    }
    assert ledger.snapshot() == before


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "BREW"])
@pytest.mark.parametrize("path", ["/healthz", "/summary"])
def test_rejects_non_get_methods(method, path):
    from lp_v2.status_server import make_handler

    config = RunConfig(Decimal("100"), AT)
    ledger, _ = prepared_ledger()
    handler = make_handler(config, ledger)
    before = ledger.snapshot()
    for target in (path, path + "?capital_usdc=200"):
        status, headers, body = request(handler, method, target, b'{"capital_usdc": "200"}')
        assert status == 405
        assert headers["Allow"] == "GET"
        assert headers.get_content_type() == "application/json"
        assert body is None if method == "HEAD" else body == {"error": "method not allowed"}
        assert ledger.snapshot() == before
        assert config == RunConfig(Decimal("100"), AT)
        assert request(handler)[2]["capital_usdc"] == "100"


def test_unknown_get_is_not_found():
    from lp_v2.status_server import make_handler

    config = RunConfig(Decimal("100"), AT)
    ledger, _ = prepared_ledger()
    before = ledger.snapshot()
    handler = make_handler(config, ledger)
    for path in ("/missing", "/config?capital_usdc=200", "/summary?capital_usdc=200"):
        status, headers, body = request(handler, path=path)
        assert status == 404 and headers.get_content_type() == "application/json"
        assert body == {"error": "not found"}
    assert ledger.snapshot() == before
    assert config == RunConfig(Decimal("100"), AT)


@pytest.mark.parametrize("interrupt", [False, True])
def test_loopback_binding(monkeypatch, interrupt):
    from lp_v2 import status_server
    from lp_v2.status_server import serve

    events = []
    config = RunConfig(Decimal("100"), AT)

    class ServerBoundary:
        def __init__(self, address, handler):
            events.append(("bind", address))
            status, _, body = request(handler)
            assert status == 200
            assert original_fields(body) == {
                "version": "0.1.0", "capital_usdc": "100", "started_at": "2026-11-02T00:00:00+00:00",
                "capital": {"inventory_cost": "0", "open_buys": "0", "pending_requests": "0",
                            "hedge_reserve": "0", "fee_reserve": "0", "total": "0"},
            }

        def __enter__(self):
            return self

        def serve_forever(self):
            events.append("serve")
            if interrupt:
                raise KeyboardInterrupt

        def __exit__(self, *exception):
            events.append("close")

    from tests.test_readonly_runtime import ScriptedReads
    monkeypatch.setattr(status_server, "create_readonly_sdk", lambda config, **kwargs: ScriptedReads())
    monkeypatch.setattr(status_server, "HTTPServer", ServerBoundary)
    assert serve(config) is None
    assert events == [("bind", ("127.0.0.1", 8765)), "serve", "close"]


def test_startup_config_is_immutable(tmp_path):
    from lp_v2.status_server import make_handler

    path = tmp_path / "capital.txt"
    path.write_text("100")
    config = parse_run(["run", "--capital-usdc", path.read_text()])
    ledger, _ = prepared_ledger()
    handler = make_handler(config, ledger)
    status, _, before = request(handler)
    assert status == 200 and before["capital_usdc"] == "100"
    assert before["started_at"] == config.started_at.isoformat()
    path.write_text("200")
    with pytest.raises(FrozenInstanceError):
        config.capital_usdc = Decimal("200")
    with pytest.raises(FrozenInstanceError):
        config.started_at = AT
    assert request(handler, "POST", "/config?capital_usdc=200", b'{"capital_usdc":"200"}')[0] == 405
    assert request(handler, path="/config?capital_usdc=200")[0] == 404
    status, _, after = request(handler)
    assert status == 200 and after == before


def test_build_summary_schema_one():
    from copy import deepcopy
    from lp_v2.status_server import build_summary
    config = RunConfig(Decimal('100'), AT)
    ledger, _ = prepared_ledger()
    rows = [dict(title='央行', condition_id='m', score='41.7', eligible=True,
                 proposed=dict(yes=dict(price='0.46', quantity='10'), no=dict(price='0.51', quantity='10'),
                               pair_cost='0.97', safety_margin='0.03', reserved_usdc='10')),
            dict(title='排除', condition_id='bad', score='38.2', eligible=False, skip_reason='中间价过高')]
    original = deepcopy(rows); before = ledger.snapshot()
    d = build_summary(config, before.capital, as_of=AT, markets=rows,
                      run=dict(code_sha='abc1234', wallet_tail='a3F9', region_check='ok'),
                      freshness=dict(decision_as_of=AT, scores_as_of=AT, wallet_as_of=AT),
                      wallet=dict(available_usdc=Decimal('182.40')),
                      safety=dict(orders_sent=0, cancels_sent=0, since=AT))
    assert (d['schema_version'], d['mode'], d['version']) == (1, 'read_only', '0.1.0')
    assert d['capital_usdc'] == d['run']['capital_usdc'] == '100'
    assert d['started_at'] == d['run']['started_at'] == '2026-11-02T00:00:00+00:00'
    assert d['as_of'] == d['freshness']['wallet_as_of'] == d['safety']['since'] == d['started_at']
    assert d['run']['code_sha'] == 'abc1234' and d['run']['wallet_tail'] == 'a3F9'
    assert d['wallet']['available_usdc'] == '182.40'
    assert d['capital'] == dict(inventory_cost='0', open_buys='0', pending_requests='9.50', hedge_reserve='0.30', fee_reserve='0.20', total='10.00', proposed_reserve='10')
    assert d['markets'][0]['proposed'] == rows[0]['proposed']
    assert d['markets'][1]['proposed'] is None
    assert set(d['markets'][0]['m4']) == {'lp_yield_pct_per_day', 'reward_today', 'rebate_today', 'reward_share', 'paired_profit', 'unpaired_inventory', 'net_yield_pct_per_day'}
    assert set(d['markets'][0]['m4'].values()) == {'UNKNOWN'}
    assert rows == original and ledger.snapshot() == before


def test_build_summary_unknown_is_not_zero():
    from lp_v2.status_server import build_summary
    ledger, _ = prepared_ledger()
    proposal = dict(yes=dict(price='0.46', quantity='10'), no=dict(price='0.51', quantity='10'), pair_cost='0.97', safety_margin='0.03', reserved_usdc='10')
    d = build_summary(RunConfig(Decimal('100'), AT), ledger.snapshot().capital,
                      markets=[dict(title='missing', eligible=True, proposed=proposal)])
    assert d['as_of'] == d['wallet']['available_usdc'] == 'UNKNOWN'
    assert set(d['freshness'].values()) == {'UNKNOWN'}
    assert set(d['safety'].values()) == {'UNKNOWN'}
    assert d['run']['code_sha'] == d['run']['wallet_tail'] == d['run']['region_check'] == 'UNKNOWN'
    assert d['capital']['inventory_cost'] == '0'
    assert d['markets'][0]['score'] == 'UNKNOWN'
    assert d['markets'][0]['proposed'] is None
    assert d['markets'][0]['eligible'] is False
    assert d['markets'][0]['skip_reason'] == '估分缺失'
    assert d['capital']['proposed_reserve'] == '0'


def test_runtime_snapshot_served_without_network():
    from tests.test_readonly_sdk import scanned_runner as ReadOnlyRunner
    from lp_v2.status_server import make_handler
    from tests.test_readonly_runtime import ScriptedReads
    from tests.test_readonly_sdk import step_at
    from datetime import timedelta
    sdk=ScriptedReads(); runner=ReadOnlyRunner(RunConfig(Decimal('100'),AT),sdk,code_sha='abc1234')
    step_at(runner,AT); calls=list(sdk.calls)
    handler=make_handler(runner.config,runner)
    d=request(handler)[2]
    assert d==runner.snapshot() and d['markets'][0]['proposed']['yes']['price']=='0.46'
    assert request(handler,path='/healthz')[2]==dict(version='0.1.0',status='ok',code_sha='abc1234',capital_usdc='100',wallet_tail='a3F9',region_check='ok')
    for _ in range(3): request(handler)
    assert sdk.calls==calls
    step_at(runner,AT+timedelta(seconds=10))
    assert request(handler)[2]['freshness']['decision_as_of']=='2026-11-02T00:00:10+00:00'


def test_health_identity_available_during_catalog_read():
    from threading import Event,Thread,Timer
    from time import monotonic
    from tests.test_readonly_sdk import scanned_runner as ReadOnlyRunner
    from lp_v2.status_server import make_handler
    from tests.test_readonly_runtime import ScriptedReads
    entered,release,published=Event(),Event(),Event()
    errors=[]
    class BlockingReads(ScriptedReads):
        def read_markets(self,now):
            entered.set()
            if not release.wait(3): raise AssertionError('catalog watchdog expired')
            return super().read_markets(now)
    runner=ReadOnlyRunner(RunConfig(Decimal('100'),AT),BlockingReads(),code_sha='abc1234',clock=lambda:AT)
    handler=make_handler(runner.config,runner)
    assert request(handler,path='/healthz')[2]['code_sha']=='abc1234'
    def stepping():
        try:
            runner.scanner.start(AT)
            runner.step(AT)
            published.set()
        except BaseException as error: errors.append(error)
    thread=Thread(target=stepping);thread.start()
    watchdog=Timer(2,release.set);watchdog.start()
    try:
        assert entered.wait(1),'read_markets not reached'
        # Scanner entry can precede the independent wallet/region publication.
        assert published.wait(1),'identity snapshot not published during blocked catalog'
        start=monotonic();health=request(handler,path='/healthz')[2]
        assert monotonic()-start<0.5,'health waited for blocked catalog'
        assert health==dict(version='0.1.0',status='ok',code_sha='abc1234',capital_usdc='100',wallet_tail='a3F9',region_check='ok')
        d=request(handler)[2]
        assert d['freshness']['decision_as_of']==d['freshness']['scores_as_of']=='UNKNOWN'
        assert d['wallet']['available_usdc']=='182.4'
        assert d['markets']==[]
    finally:
        release.set();watchdog.cancel();watchdog.join();thread.join(2)
    assert not thread.is_alive() and not errors
    assert runner.scanner.join(2)
    from datetime import timedelta
    ready_at=AT+timedelta(seconds=10)
    runner.clock=lambda:ready_at
    runner.step(ready_at)
    assert request(handler)[2]['freshness']['decision_as_of']==ready_at.isoformat()
    assert runner.scanner.stop(2)


def test_rotation_scan_summary():
    from decimal import Decimal as D
    from lp_v2.models import CapitalBreakdown
    from lp_v2.status_server import build_summary
    from lp_v2.cli import RunConfig
    from datetime import timedelta
    import json

    scan = dict(started_at=AT, finished_at=AT+timedelta(seconds=115), duration_s=115,
        directory_duration_s=100, market_duration_s=15, pages=115, rows=11411,
        requests=130, prescreened=5, reached_end=False, errors=[dict(page=116,reason='cursor_invalid')])
    rotation = dict(current='a', candidate='b', phase='exiting', reason='waiting_inventory',
        score_version='lp-v2-score-1', score_at=AT, switch_count=3, cancel_count=6, scan=scan)
    summary = build_summary(RunConfig(D('100'),AT), CapitalBreakdown(*(D('0') for _ in range(6))), rotation=rotation)
    result = json.loads(json.dumps(summary))['rotation']
    assert result['scan'] == dict(scan,started_at=AT.isoformat(),finished_at=(AT+timedelta(seconds=115)).isoformat())
    assert {k:result[k] for k in ('current','candidate','phase','reason','switch_count','cancel_count')} == dict(
        current='a',candidate='b',phase='exiting',reason='waiting_inventory',switch_count=3,cancel_count=6)
    assert result['coverage_label'] == '已扫范围内第一'
    assert result['score_at'] == AT.isoformat()
    assert 'plan' not in json.dumps(result)
    scan['reached_end'] = True
    with_errors = build_summary(RunConfig(D('100'),AT), CapitalBreakdown(*(D('0') for _ in range(6))), rotation=rotation)
    assert with_errors['rotation']['coverage_label'] == '已扫范围内第一'
    scan['errors'] = []
    complete = build_summary(RunConfig(D('100'),AT), CapitalBreakdown(*(D('0') for _ in range(6))), rotation=rotation)
    assert complete['rotation']['coverage_label'] == '政治类完整扫描第一'


def test_blocked_region_remains_visible_in_health_and_summary():
    from lp_v2.readonly_runtime import ReadOnlyRunner
    from lp_v2.status_server import make_handler
    from tests.test_readonly_runtime import BlockedRegionReads
    from tests.test_readonly_sdk import step_at

    class ObservedReads(BlockedRegionReads):
        def read_orders(self, now):
            self.calls.append(('orders', now))
            return super().read_orders(now)

        def wallet_tail(self):
            self.calls.append(('wallet_tail', None))
            return super().wallet_tail()

        def transport_safety(self):
            self.calls.append(('transport_safety', None))
            return super().transport_safety()

        def safety(self, since):
            self.calls.append(('safety', since))
            return super().safety(since)

    sdk = ObservedReads()
    runner = ReadOnlyRunner(RunConfig(Decimal('100'), AT), sdk, code_sha='abc1234')
    step_at(runner, AT)
    calls = list(sdk.calls)
    handler = make_handler(runner.config, runner)
    status, _, health = request(handler, path='/healthz')
    assert status == 200
    assert health == dict(version='0.1.0', status='ok', code_sha='abc1234',
                          capital_usdc='100', wallet_tail='a3F9', region_check='blocked')
    status, _, summary = request(handler, path='/summary')
    assert status == 200
    assert summary['mode'] == 'read_only'
    assert summary['run']['region_check'] == 'blocked'
    assert summary == runner.snapshot()
    assert sdk.calls == calls
