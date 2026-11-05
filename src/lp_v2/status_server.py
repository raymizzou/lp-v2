"""Read-only local status from frozen startup configuration and current ledger facts."""

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import PackageNotFoundError, version
import json
from typing import TYPE_CHECKING

from lp_v2.ledger import Ledger

if TYPE_CHECKING:
    from lp_v2.cli import RunConfig

try:
    VERSION = version("lp-v2")
except PackageNotFoundError:
    VERSION = "0+unknown"


def make_handler(config: "RunConfig", ledger: Ledger) -> type[BaseHTTPRequestHandler]:
    def current_snapshot():
        snapshot = ledger.snapshot()
        return snapshot if isinstance(snapshot, dict) else build_summary(config, snapshot.capital)

    class StatusHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def parse_request(self):
            if not super().parse_request():
                return False
            if self.command != "GET":
                self._send_json(405, {"error": "method not allowed"})
                return False
            return True

        def do_GET(self):
            if self.path == "/healthz":
                snapshot = current_snapshot()
                run = snapshot['run']
                self._send_json(200, dict(version=VERSION, status='ok',
                    code_sha=run['code_sha'], capital_usdc=snapshot['capital_usdc'],
                    wallet_tail=run['wallet_tail'], region_check=run['region_check']))
            elif self.path == "/summary":
                self._send_json(200, current_snapshot())
            elif self.path in ('/', '/dashboard', '/dashboard?demo=1', '/?demo=1', '/dashboard.js'):
                from importlib.resources import files
                name = 'dashboard.js' if self.path == '/dashboard.js' else 'index.html'
                body = files('lp_v2').joinpath('dashboard', name).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json(404, {"error": "not found"})

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if status == 405:
                self.send_header("Allow", "GET")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    return StatusHandler


def serve(config: "RunConfig", *, runner=None) -> None:
    if config.live:
        return _serve_live(config,runner=runner)
    from threading import Thread, Event
    from lp_v2.readonly_runtime import ReadOnlyRunner
    if runner is None:
        stop = Event()
        runner = ReadOnlyRunner(config, create_readonly_sdk(config, stop=stop), code_sha=config.code_sha, stop=stop)
    thread = Thread(target=runner.run, name='lp-v2-read-only')
    thread.start()
    try:
        with HTTPServer(("127.0.0.1", 8765), make_handler(config, runner)) as server:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        runner.stop.set()
        thread.join()


def create_readonly_sdk(config, *, stop=None):
    from lp_v2.readonly_sdk import ReadOnlySDK
    return ReadOnlySDK(credentials=config.credentials, stop=stop)


UNKNOWN = 'UNKNOWN'
M4_FIELDS = ('lp_yield_pct_per_day', 'reward_today', 'rebate_today', 'reward_share',
             'paired_profit', 'unpaired_inventory', 'net_yield_pct_per_day')


def build_summary(config, capital, *, as_of=None, run=None, freshness=None,
                  wallet=None, wallet_orders=None, safety=None, transport_safety=None, markets=()):
    """Serialize supplied facts without reading clocks, I/O, or mutating inputs."""
    from copy import deepcopy
    from datetime import datetime
    from decimal import Decimal
    def value(v):
        if v is None:
            return UNKNOWN
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, Decimal):
            return str(v)
        return v
    run, freshness, wallet, safety = run or {}, freshness or {}, wallet or {}, safety or {}
    wallet_orders = wallet_orders or {}
    transport_safety = transport_safety or {}
    rows = []
    reserve = Decimal('0')
    for market in markets:
        row = {key: value(market.get(key)) for key in ('title', 'condition_id', 'score', 'eligible', 'skip_reason')}
        row['skip_reason'] = market.get('skip_reason')
        row['proposed'] = deepcopy(market.get('proposed')) if row['eligible'] is True else None
        if row['score'] == UNKNOWN:
            row.update(eligible=False, proposed=None, skip_reason=row['skip_reason'] or '估分缺失')
        if row['proposed'] is not None:
            reserve += Decimal(row['proposed']['yes']['quantity'])
        row['m4'] = {key: UNKNOWN for key in M4_FIELDS}
        rows.append(row)
    return dict(version=VERSION, schema_version=1, mode='read_only', as_of=value(as_of),
                capital_usdc=str(config.capital_usdc), started_at=config.started_at.isoformat(),
                run=dict(code_sha=value(run.get('code_sha')), wallet_tail=value(run.get('wallet_tail')),
                         region_check=value(run.get('region_check')), started_at=config.started_at.isoformat(),
                         capital_usdc=str(config.capital_usdc)),
                freshness={key: value(freshness.get(key)) for key in ('decision_as_of', 'scores_as_of', 'wallet_as_of')},
                wallet=dict(available_usdc=value(wallet.get('available_usdc'))),
                wallet_orders={key: value(wallet_orders.get(key)) for key in
                               ('legacy_order_count', 'as_of', 'last_success_at')},
                transport_safety={key: value(transport_safety.get(key)) for key in
                                  ('get_attempts', 'rejected_non_get_attempts', 'status', 'monitor_started_at')},
                capital={**{key: str(v) for key, v in asdict(capital).items()}, 'proposed_reserve': str(reserve)},
                safety={key: value(safety.get(key)) for key in ('orders_sent', 'cancels_sent', 'since')}, markets=rows)


def _serve_live(config, *, runner=None):
    import signal
    from threading import Thread
    if runner is None:
        from lp_v2.live_runtime import create_live_runner
        runner = create_live_runner(config)
    previous = {sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
    for sig in previous:
        signal.signal(sig,lambda number,frame:runner.stop.set())
    thread = Thread(target=runner.run,name='lp-v2-live')
    try:
        with HTTPServer(('127.0.0.1',8765),make_handler(config,runner)) as server:
            server.timeout = 1
            thread.start()
            try:
                while thread.is_alive() and not runner.stop.is_set():
                    server.handle_request()
            finally:
                runner.stop.set()
                thread.join()
    finally:
        if thread.ident is None:
            runner.sdk.close()
            if hasattr(runner.observations,'close'):
                runner.observations.close()
        for sig,handler in previous.items():
            signal.signal(sig,handler)
    return runner.exit_code if type(runner.exit_code) is int else 1
