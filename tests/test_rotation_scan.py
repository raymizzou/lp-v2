from dataclasses import replace
from datetime import timedelta
from threading import Event, Lock

import httpx
import pytest
from polymarket import PublicClient
from polymarket.clients._transport import SyncTransport

from tests.test_readonly_sdk import AT, CID, market_wire


def scan_boundary(*, pages=115, fail=None, cursor_problem=None, latch=None):
    current = [AT]
    seen = []
    concurrency = dict(active=0, maximum=0)
    lock = Lock()

    def handle(request):
        with lock:
            concurrency['active'] += 1
            concurrency['maximum'] = max(concurrency.values())
        try:
            seen.append(request)
            assert request.method == 'GET'
            current[0] += timedelta(seconds=1)
            if request.url.path == '/markets/keyset':
                assert request.url.params['limit'] == '100'
                assert request.url.params['tag_id'] == '2'
                assert request.url.params['closed'] == 'false'
                if len(seen) > 120:
                    raise httpx.ReadTimeout('bounded fake watchdog')
                page = int(request.url.params.get('after_cursor', '1'))
                if latch:
                    latch[0].set()
                    assert latch[1].wait(3), 'scan latch watchdog'
                if page == fail:
                    raise httpx.ReadTimeout('SECRET_SENTINEL')
                cid = '0x'+f'{page:064x}'
                wire = market_wire(cid, liquidityNum='500')
                cursor = str(page+1) if page < pages else None
                data = dict(markets=[wire], next_cursor=cursor)
                if page == 3:
                    if cursor_problem == 'repeat': data['next_cursor']='2'
                    if cursor_problem == 'missing': data.pop('next_cursor')
                    if cursor_problem == 'truncated': data['next_cursor']=''
                return httpx.Response(200, json=data)
            raise AssertionError(str(request.url))
        finally:
            with lock: concurrency['active'] -= 1

    public = PublicClient()
    for name in ('gamma', 'clob'):
        getattr(public._ctx, name).close()
        public._ctx = replace(public._ctx, **{name: SyncTransport(base_url='https://'+name+'.polymarket.com',
            client=httpx.Client(base_url='https://'+name+'.polymarket.com', transport=httpx.MockTransport(handle)))})
    from lp_v2.readonly_sdk import ReadOnlySDK
    sdk = ReadOnlySDK(public=public, http=httpx.Client(transport=httpx.MockTransport(handle)), clock=lambda:current[0])
    return sdk, current, seen, concurrency


def test_complete_political_scan_115_pages():
    sdk, clock, seen, concurrency = scan_boundary()
    try:
        result = sdk.scan_markets(AT)
        assert len(result.rows) == 115
        assert result.scan['pages'] == result.scan['rows'] == result.scan['requests'] == 115
        assert result.scan['started_at'] == AT
        assert result.scan['finished_at'] == AT+timedelta(seconds=115)
        assert result.scan['duration_s'] == 115
        assert result.scan['directory_duration_s'] == 115
        assert result.scan['market_duration_s'] == 0
        assert result.scan['prescreened'] == 0
        assert result.scan['reached_end'] is True and result.scan['errors'] == []
        assert concurrency['maximum'] == result.scan['max_inflight'] == 1
        assert len(seen) == 115
    finally: sdk.close()


@pytest.mark.parametrize('failure', ['page60', 'repeat', 'missing', 'truncated'])
def test_failed_page_and_broken_cursor_are_incomplete(failure):
    sdk, clock, seen, concurrency = scan_boundary(fail=60 if failure=='page60' else None,
        cursor_problem=None if failure=='page60' else failure)
    try:
        result = sdk.scan_markets(AT)
        assert result.scan['reached_end'] is False
        assert not result.complete
        assert result.scan['pages'] == len(result.rows) == (59 if failure=='page60' else 3)
        assert result.scan['errors'] == [dict(page=60 if failure=='page60' else 3,
            reason='directory_request_failed' if failure=='page60' else 'cursor_invalid')]
        assert 'SECRET_SENTINEL' not in repr(result)
        assert concurrency['maximum'] == 1
        assert len(seen) == (60 if failure=='page60' else 3)
    finally: sdk.close()


def test_scan_worker_never_overlaps_or_blocks_decisions():
    from concurrent.futures import ThreadPoolExecutor
    from decimal import Decimal as D
    from lp_v2.cli import RunConfig
    from lp_v2.readonly_runtime import ReadOnlyRunner
    from lp_v2.rotation_scan import ScanWorker
    from tests.test_readonly_runtime import ScriptedReads

    entered, release = Event(), Event()
    sdk, current, seen, concurrency = scan_boundary(pages=1, latch=(entered,release))
    worker = ScanWorker(sdk, clock=lambda:current[0])
    runner = ReadOnlyRunner(RunConfig(D('100'),AT), ScriptedReads(), scanner=worker, clock=lambda:AT)
    try:
        assert worker.start(AT)
        assert entered.wait(2)
        assert worker.start(AT) is False
        with ThreadPoolExecutor(max_workers=1) as executor:
            report = executor.submit(runner.step, AT).result(timeout=1)
            assert report['capital']['total'] == '0'
            assert report['markets'] == []
            assert executor.submit(runner.snapshot).result(timeout=1) == report
        assert len(seen) == 1
        release.set()
        assert worker.join(2)
        result = worker.latest()
        assert result.complete and result.scan['pages'] == 1
        result.scan['pages'] = 999
        assert worker.latest().scan['pages'] == 1
        assert worker.stop(2)
        assert not worker.start(AT)
        assert concurrency['maximum'] == 1
    finally:
        release.set()
        worker.stop(3)
        sdk.close()
