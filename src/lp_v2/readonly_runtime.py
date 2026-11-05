"""Read-only decisions and atomic memory publication; never execute intents."""
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event, Lock

from lp_v2.engine import Engine
from lp_v2.ledger import Ledger
from lp_v2.models import Balance, CapitalBreakdown
from lp_v2.scoring import score_market, eligible_market
from lp_v2.capital import can_open_market
from lp_v2.status_server import build_summary, UNKNOWN


class ReadOnlyRunner:
    def __init__(self, config, sdk, *, code_sha=UNKNOWN, clock=lambda: datetime.now(UTC), stop=None):
        self.config, self.sdk, self.clock = config, sdk, clock
        self.stop = stop if stop is not None else Event()
        self.__lock = Lock()
        self.__run = dict(code_sha=code_sha, wallet_tail=sdk.wallet_tail(), region_check=UNKNOWN)
        self.__zero = CapitalBreakdown(*(Decimal('0') for _ in range(6)))
        self.__freshness = {}
        self.__rows = []
        self.__next = {}
        self.__scores = {}
        self.__score_started_at = None
        self.__proposals = {}
        self.__engine = Engine(Ledger("lp-v2:simulation", config.capital_usdc))
        self.__wallet = {}
        self.__wallet_orders = {}
        self.__snapshot = self.__build(None)

    def __build(self, now):
        return build_summary(self.config, self.__zero, as_of=now, run=self.__run,
                             freshness=self.__freshness, wallet=self.__wallet,
                             wallet_orders=self.__wallet_orders,
                             transport_safety=self.sdk.transport_safety(),
                             safety=self.sdk.safety(self.config.started_at), markets=self.__rows)

    def snapshot(self):
        with self.__lock:
            return deepcopy(self.__snapshot)

    def step(self, now=None):
        now = self.clock() if now is None else now
        elapsed = (now - self.config.started_at).total_seconds()
        def due(name, seconds):
            if elapsed < self.__next.get(name, float('-inf')):
                return False
            self.__next[name] = (elapsed // seconds + 1) * seconds
            return True
        if due('wallet', 30):
            try:
                self.__wallet = self.sdk.read_wallet(now)
            except Exception:
                self.__wallet = dict(available_usdc=UNKNOWN, as_of=None)
            if self.__wallet.get('as_of') is not None:
                self.__freshness['wallet_as_of'] = self.__wallet['as_of']
            try:
                orders = self.sdk.read_orders(now)
                count = orders['legacy_order_count']
                if type(count) is not int or count < 0 or orders['as_of'] != now:
                    raise ValueError('orders unavailable')
                self.__wallet_orders.update(legacy_order_count=count, as_of=now, last_success_at=now)
            except Exception:
                self.__wallet_orders.update(legacy_order_count=UNKNOWN, as_of=None)
        score_at = self.__score_started_at
        round_started = now
        score_due = score_at is None or (now - score_at).total_seconds() >= 600
        if not due('decision', 10):
            return self.snapshot()
        try:
            self.__run['region_check'] = self.sdk.read_region()
            if self.__run['region_check'] != 'ok':
                for entry in self.__proposals.values():
                    entry['quotes'] = None
                for row in self.__rows:
                    row.update(proposed=None, eligible=False, skip_reason='地区检查未通过或未知')
            # Publish observed identity independently of the slower catalog.
            # Decision/score success times remain untouched until computation.
            with self.__lock:
                self.__snapshot = self.__build(now)
            rows = self.sdk.read_markets(now)
            completed = self.clock()
            if (not isinstance(completed,datetime) or completed.tzinfo is None or completed.utcoffset() is None or completed < now):
                raise ValueError('local observation clock unavailable or backward')
            for row in rows:
                if row.get('plan') is not None:
                    observed = row.get('observation_completed_at')
                    if (not isinstance(observed,datetime) or observed.tzinfo is None
                            or observed.utcoffset() is None or not now <= observed <= completed):
                        raise ValueError('market observation completion unavailable or discontinuous')
            now = completed
        except Exception:
            self.__run['region_check'] = UNKNOWN
            for entry in self.__proposals.values():
                entry['quotes'] = None
            for row in self.__rows:
                row.update(proposed=None, eligible=False, skip_reason='市场读取失败')
            with self.__lock:
                self.__snapshot = self.__build(now)
            return self.snapshot()
        if score_due:
            scores = {}
            for row in rows:
                source = (str(score_market(row['plan'].market).score) if row.get('plan') is not None
                          else row.get('score', UNKNOWN))
                try:
                    numeric = Decimal(str(source))
                    if numeric.is_finite() and numeric >= 0:
                        scores[row['condition_id']] = str(source)
                except ArithmeticError:
                    pass
            self.__scores = scores
            if scores or not rows:
                self.__freshness['scores_as_of'] = now
                self.__score_started_at = round_started
        for row in rows:
            row['score'] = self.__scores.get(row['condition_id'], UNKNOWN)
        # A new simulation per round avoids carrying unexecuted pending orders
        # forward as reality. This ledger is never exposed as actual occupation.
        ledger = Ledger('lp-v2:simulation', self.config.capital_usdc,
                        balance=Balance(self.config.capital_usdc, now))
        plans = [r['plan'] for r in rows if r.get('plan') is not None and r.get('eligible') is True and r['score'] != UNKNOWN and self.__run['region_check'] == 'ok']
        self.__engine.ledger = ledger
        decision = self.__engine.decide(plans, now=now)
        occupied = risk = Decimal('0')
        present = {r['condition_id'] for r in rows}
        for cid, entry in self.__proposals.items():
            if cid not in present:
                entry['quotes'] = None
        for row in rows:
            p = row.get('plan')
            cid = row['condition_id']
            previous = self.__proposals.get(cid)
            row['proposed'] = None
            if p is None:
                if previous:
                    previous['quotes'] = None
                continue
            fresh = tuple(r.quote for r in decision.place if r.quote.condition_id == cid)
            waiting = previous is not None and (now-previous['at']).total_seconds() < 60
            pair = previous['quotes'] if waiting else fresh
            allowed = (row.get('eligible') is True and row['score'] != UNKNOWN
                       and self.__run['region_check'] == 'ok' and self.__safe_pair(pair, p))
            if allowed:
                quantity = pair[0].quantity
                pair_risk = max((q.price + fee) * quantity for q, fee in zip(pair, p.fee_per_share))
                allowed = can_open_market(startup_cap=self.config.capital_usdc, occupied=occupied,
                    planned_shares=quantity, projected_market_unpaired=quantity,
                    current_unpaired_cost=risk, proposed_unpaired_cost=pair_risk)
            if not allowed:
                if previous:
                    previous['quotes'] = None
                row.update(eligible=False, skip_reason=row.get('skip_reason') or '拟挂不再满足安全、身份或资金条件')
                continue
            if previous is None or pair != previous['quotes']:
                self.__proposals[cid] = dict(quotes=pair, at=now)
            occupied += quantity
            risk += pair_risk
            yes, no = pair
            cost = yes.price + no.price + sum(p.fee_per_share)
            row['proposed'] = dict(yes=dict(price=str(yes.price),quantity=str(quantity)),
                no=dict(price=str(no.price),quantity=str(quantity)), pair_cost=str(cost),
                safety_margin=str(1-cost),reserved_usdc=str(quantity))
            row.update(eligible=True,skip_reason=None)
        self.__rows = rows
        self.__freshness['decision_as_of'] = now
        with self.__lock:
            self.__snapshot = self.__build(now)
        return self.snapshot()

    @staticmethod
    def __safe_pair(pair, plan):
        """A retained proposal must satisfy current facts, before any age rule."""
        try:
            m = plan.market
            if (not pair or len(pair) != 2 or len(plan.books) != 2
                    or len(plan.fee_per_share) != 2 or not eligible_market(m)):
                return False
            if tuple(q.token_id for q in pair) != tuple(t.token_id for t in m.tokens):
                return False
            if pair[0].quantity != pair[1].quantity or pair[0].quantity != plan.quantity:
                return False
            if plan.quantity < max(m.min_order_size, m.rewards_min_size) or plan.quantity <= 0:
                return False
            band = Decimal(str(m.rewards_max_spread)) / 100
            for quote, book, fee in zip(pair, plan.books, plan.fee_per_share):
                if (quote.condition_id != m.condition_id or book.condition_id != m.condition_id
                        or quote.token_id != book.token_id or not quote.price.is_finite()
                        or not fee.is_finite() or fee < 0 or m.tick_size <= 0
                        or quote.price % m.tick_size != 0 or not 0 < quote.price < book.best_ask
                        or abs(quote.price - (book.best_bid + book.best_ask)/2) > band):
                    return False
            return pair[0].price + pair[1].price + sum(plan.fee_per_share) <= Decimal('0.98')
        except (ValueError, TypeError, ArithmeticError, AttributeError):
            return False

    def run(self):
        try:
            while not self.stop.is_set():
                self.step()
                self.stop.wait(1)
        finally:
            self.sdk.close()
