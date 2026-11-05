"""Completed scan snapshots, separate from the ten-second trading decision loop."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScanRound:
    rows: tuple
    scan: dict

    @property
    def complete(self):
        return self.scan.get('reached_end') is True and not self.scan.get('errors')


class ScanWorker:
    """One sequential round at a time; publish copies only after completion."""
    def __init__(self, sdk, *, clock=None):
        from datetime import UTC, datetime
        from threading import Event, Lock
        self.sdk = sdk
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock, self._stop = Lock(), Event()
        self._thread = None
        self._latest = self._complete = None

    def scan_once(self, now=None):
        """Synchronous scan entry for offline tools; never concurrent with start."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError('scan already running')
            return self._scan(self.clock() if now is None else now)

    def _scan(self, now):
        from copy import deepcopy
        result = self._perform(now)
        self._latest = deepcopy(result)
        if result.complete:
            self._complete = deepcopy(result)
        return deepcopy(result)

    def _perform(self, now):
        try:
            return self.sdk.scan_markets(now)
        except Exception:
            return ScanRound((),dict(started_at=now,finished_at=None,duration_s=None,
                directory_duration_s=None,market_duration_s=None,pages=0,rows=0,requests=None,
                prescreened=0,reached_end=False,errors=[dict(reason='scan_unavailable')]))

    def start(self, now=None):
        from threading import Thread
        with self._lock:
            if self._stop.is_set() or (self._thread is not None and self._thread.is_alive()):
                return False
            def run():
                from copy import deepcopy
                try:
                    result = self._perform(self.clock() if now is None else now)
                except Exception:
                    return  # Preserve last finished snapshot, never invent full coverage.
                with self._lock:
                    self._latest = deepcopy(result)
                    if result.complete:
                        self._complete = deepcopy(result)
            self._thread = Thread(target=run, name='lp-v2-politics-scan', daemon=True)
            self._thread.start()
            return True

    def latest(self, *, complete=False):
        from copy import deepcopy
        with self._lock:
            return deepcopy(self._complete if complete else self._latest)

    def join(self, timeout=None):
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def stop(self, timeout=None):
        self._stop.set()
        if hasattr(self.sdk, 'request_stop'):
            self.sdk.request_stop()
        return self.join(timeout)
