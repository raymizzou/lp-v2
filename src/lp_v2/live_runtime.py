"""One fixed-market executor. Exchange readback always precedes decisions."""
from copy import deepcopy
from datetime import UTC, datetime
from dataclasses import asdict
from decimal import Decimal as D
from threading import Event, Lock

from lp_v2.engine import Engine
from lp_v2.ledger import Ledger
from lp_v2.status_server import build_summary, UNKNOWN, M4_FIELDS


class LiveRunner:
    def __init__(self, config, sdk, observations, *, clock=lambda: datetime.now(UTC), stop=None):
        self.config, self.sdk, self.observations, self.clock = config, sdk, observations, clock
        self._condition_id = sdk.condition_id
        self._reconciled_late_fills = 0
        self.ledger = Ledger(sdk.owner, config.capital_usdc)
        self.engine = Engine(self.ledger)
        self.stop = stop if stop is not None else Event()
        self._lock = Lock()
        self._next = None
        self._rows = []
        self._last_plans = ()
        self._accounting_at = None
        self._accounting_status = UNKNOWN
        self._income_at = None
        self._safety_cancels = {}
        self._opening_cancels = {}
        self._region = UNKNOWN
        self._m4 = {key: UNKNOWN for key in (*M4_FIELDS,"net_profit")}
        self._freshness = {}
        self._valuation_at = None
        self.exit_code = None
        self.stop_timeout_s = 90
        self._snapshot = self._build(None)

    @property
    def condition_id(self):
        return self._condition_id

    def _build(self, now):
        state = self.ledger.snapshot()
        summary = build_summary(self.config, state.capital, as_of=now,
            run=dict(code_sha=self.config.code_sha,wallet_tail=self.sdk.wallet[-4:],region_check=self._region),
            wallet=dict(available_usdc=state.cash), markets=self._rows,freshness=self._freshness)
        for row in summary['markets']:
            row['m4'] = {k:str(v) for k,v in self._m4.items()}
        summary['mode'] = 'live'
        summary['accounting'] = dict(status=self._accounting_status,as_of=self._accounting_at.isoformat() if self._accounting_at is not None else UNKNOWN)
        summary['freshness']['income_as_of'] = self._income_at.isoformat() if self._income_at is not None else UNKNOWN
        summary['safety'] = dict(orders_sent=len(self.sdk.journal.events('signed')),
            cancels_sent=len(self.sdk.journal.events('cancel_intent')),since=self.config.started_at.isoformat())
        return summary

    def snapshot(self):
        with self._lock:
            return deepcopy(self._snapshot)

    def step(self, now=None, *, allow_open=True, opening_policy=None):
        now = self.clock() if now is None else now
        if self._next is not None and (now-self._next).total_seconds() < 10:
            return self.snapshot()
        self._next = now
        with self.sdk.read_cycle(now=now):
            reconciled = False
            self._region = UNKNOWN
            plans = ()
            try:
                self._reconcile()
                reconciled = self.sdk.orders_verified()
                if not reconciled:
                    raise ValueError('owned order readback remains uncertain')
                self._accounting_at = now
                self._rows = [r for r in (self.observations.read_fixed_market(now,self.condition_id) if hasattr(self.observations,'read_fixed_market') else self.observations.read_markets(now)) if r['condition_id'] == self.condition_id]
                accounting_at = now
                completed = self.clock()
                if (not isinstance(completed,datetime) or completed.tzinfo is None or completed.utcoffset() is None or completed < now):
                    raise ValueError('local observation clock unavailable or backward')
                for row in self._rows:
                    if row.get('plan') is not None:
                        observed = row.get('observation_completed_at')
                        if (not isinstance(observed,datetime) or observed.tzinfo is None
                                or observed.utcoffset() is None or not now <= observed <= completed):
                            raise ValueError('market observation completion unavailable or discontinuous')
                now = completed
                plans = tuple(r['plan'] for r in self._rows if r.get('plan') is not None)
                self._last_plans = plans
                if any(tuple(t.token_id for t in p.market.tokens) != self.sdk.token_ids for p in plans):
                    raise ValueError('frozen token identity changed')
                region = self.observations.read_region_policy()
                blocked = region.get('blocked')
                country = region.get('country')
                self._region = 'ok' if blocked is False else 'blocked' if blocked is True else UNKNOWN
                allowed = (type(blocked) is bool and isinstance(country, str) and len(country) == 2
                           and (blocked is False or (country == 'JP' and self.sdk.client.get_closed_only_mode() is False)))
                if not allowed:
                    self._cancel_owned(now.timestamp(),self._safety_cancels,at=now)
                    self.sdk.journal.append('region_denied', dict(at=now,region_check=self._region))
                    for row in self._rows:
                        row.update(eligible=False,skip_reason='地区检查未通过或未知')
                if opening_policy is not None:
                    from lp_v2.scoring import eligible_market
                    valid = allowed and bool(plans) and all(eligible_market(p.market) for p in plans)
                    allow_open = opening_policy(valid, now)
                decision = self.engine.decide(plans, now=now, allow_open=allow_open) if allowed and not self.stop.is_set() else None
                if decision is not None:
                    for c in decision.cancel:
                        self._opening_cancels[c.order_id] = now.timestamp()
                        self.ledger.apply_order(self.sdk.cancel_order(c.order_id, at=now))
                    if not allow_open:
                        from lp_v2.models import OrderStatus
                        for order in self.ledger.snapshot().orders:
                            if (order.request.purpose == 'opening'
                                    and (not order.authoritative or order.status == OrderStatus.LIVE)
                                    and now.timestamp()-self._opening_cancels.get(order.order_id,float('-inf')) >= 5):
                                self._opening_cancels[order.order_id] = now.timestamp()
                                self.ledger.apply_order(self.sdk.cancel_order(order.order_id,at=now))
                    for request in decision.place:
                        self.ledger.apply_order(self.sdk.place_order(request, at=now))
                    for request in decision.merge:
                        self.ledger.apply_merge(self.sdk.merge(request, at=now))
                self._m4 = self._income_fields(now,plans)
                self._freshness = dict(decision_as_of=now,scores_as_of=now,wallet_as_of=accounting_at)

            except Exception:
                if opening_policy is not None:
                    opening_policy(False, now)
                self._m4 = {key:UNKNOWN for key in (*M4_FIELDS,'net_profit')}
                self._valuation_at = self._income_at = None
                self._cancel_owned(now.timestamp(),self._safety_cancels,at=now)
                self.sdk.journal.append('diagnostic', dict(at=now,reason='round readback or execution unavailable'))
                for row in self._rows:
                    row.update(eligible=False,skip_reason='读回未核实，暂停下单')
            self._accounting_status = 'SUCCESS' if reconciled else UNKNOWN
            self.sdk.journal.append('occupation',self._audit(now,reconciled,plans))
            with self._lock:
                self._snapshot = self._build(now)
            return self.snapshot()

    def _reconcile(self):
        """Rebuild only from a newly authenticated, complete late-fill revision.

        Ledger's direct contradictory-terminal guards remain intact. A fresh
        ledger must independently reconcile every order, trade, merge and chain
        balance before replacing the previous owned snapshot.
        """
        try:
            self.ledger.reconcile(self.sdk)
        except ValueError:
            revisions = len(self.sdk.journal.events('late_fill_reconciliation'))
            if revisions <= self._reconciled_late_fills:
                raise
            candidate = Ledger(self.sdk.owner,self.config.capital_usdc)
            candidate.reconcile(self.sdk)
            self.ledger = candidate
            self.engine.ledger = candidate
        self._reconciled_late_fills = len(self.sdk.journal.events('late_fill_reconciliation'))

    def _audit(self, now, reconciled, plans=()):
        from lp_v2.models import Outcome,OrderStatus
        state = self.ledger.snapshot()
        books = {b.token_id:b for p in plans for b in p.books}
        pair = min((next((p.quantity for p in state.positions if p.token_id == token),D('0')) for token in self.sdk.token_ids),default=D('0'))
        inventory = []
        for p in state.positions:
            if p.quantity == 0:
                continue
            unmatched = p.quantity-pair
            book = books.get(p.token_id)
            fresh = (reconciled and book is not None and book.condition_id == self.condition_id
                     and 0 <= (now-book.observed_at).total_seconds() <= 60)
            mid = (book.best_bid+book.best_ask)/2 if fresh else None
            inventory.append(dict(token_id=p.token_id,quantity=p.quantity,cost_usdc=p.cost_usdc,
                unpaired_quantity=unmatched,unpaired_cost_usdc=p.cost_usdc*unmatched/p.quantity,
                mark_usdc=mid*unmatched if mid is not None else UNKNOWN,
                mark_at=book.observed_at if fresh else None))
        return dict(at=now,condition_id=self.condition_id,**asdict(state.capital),
            reconciliation='SUCCESS' if reconciled else UNKNOWN,accounting_as_of=self._accounting_at,
            inventory=tuple(inventory),uncertainty=dict(pending=tuple(r.request_id for r in state.pending),
                orders=tuple(o.order_id for o in state.orders if not o.authoritative),
                merges=tuple(r.request.merge_id for r in state.merges if r.outcome == Outcome.UNKNOWN)))

    def _income_fields(self, now, plans):
        from lp_v2.models import Fill
        day = now.astimezone(UTC).date()
        metrics = {k:UNKNOWN for k in (*M4_FIELDS,'net_profit')}
        income = self.sdk.read_income(now)
        self._income_at = income["at"]
        metrics.update({k:income[k] for k in ('reward_today','rebate_today','reward_share')})
        state = self.ledger.snapshot()
        # Replay gross purchase basis; fees are deducted exactly once in net profit.
        basis, fees, paired = {}, D('0'), D('0')
        events = [(f.executed_at,0,f.trade_id,f) for f in state.fills]
        events += [(r.observed_at,1,r.request.merge_id,r) for r in state.merges if r.received_usdc]
        for at,_,_,fact in sorted(events):
            if isinstance(fact,Fill):
                q,c = basis.get(fact.token_id,(D('0'),D('0')))
                basis[fact.token_id] = q+fact.quantity,c+fact.quantity*fact.price
                if at.astimezone(UTC).date() == day:
                    fees += fact.fee_usdc
            else:
                cost = D('0')
                for leg in fact.consumed_positions:
                    q,c = basis[leg.token_id]
                    consumed = c if q == leg.quantity else c*leg.quantity/q
                    basis[leg.token_id] = q-leg.quantity,c-consumed
                    cost += consumed
                if at.astimezone(UTC).date() == day:
                    paired += fact.received_usdc-cost
        metrics['paired_profit'] = paired
        quantities = [basis.get(t,(D('0'),D('0')))[0] for t in self.sdk.token_ids]
        metrics['unpaired_inventory'] = abs(quantities[0]-quantities[1])
        books = {b.token_id:b for p in plans for b in p.books}
        inventory_pnl = None
        fresh_books = {t:b for t,b in books.items() if 0 <= (now-b.observed_at).total_seconds() <= 60}
        if all(q == 0 or token in fresh_books for token,(q,c) in basis.items()):
            inventory_pnl = sum(((fresh_books[token].best_bid+fresh_books[token].best_ask)/2*q-c for token,(q,c) in basis.items() if q),D('0'))
        baselines = [p for _,p in self.sdk.journal.events('day_baseline') if p['day'] == day.isoformat()]
        if not baselines:
            # Midnight marks for carried inventory cannot be invented after downtime.
            before = [(at,kind,key,f) for at,kind,key,f in events if at.astimezone(UTC).date() < day]
            old_basis = {}
            for _,_,_,f in before:
                if isinstance(f,Fill):
                    q,c = old_basis.get(f.token_id,(D('0'),D('0')))
                    old_basis[f.token_id] = q+f.quantity,c+f.quantity*f.price
                else:
                    for leg in f.consumed_positions:
                        q,c = old_basis[leg.token_id]
                        consumed = c if q == leg.quantity else c*leg.quantity/q
                        old_basis[leg.token_id] = q-leg.quantity,c-consumed
            baseline = D('0') if not any(q for q,c in old_basis.values()) else None
            continuous = (self._valuation_at is not None and 0 < (now-self._valuation_at).total_seconds() <= 60
                          and (day-self._valuation_at.astimezone(UTC).date()).days == 1)
            observed = continuous and all(q == 0 or t in fresh_books for t,(q,c) in old_basis.items())
            mark_at = None
            if baseline is None and observed:
                baseline = sum(((fresh_books[t].best_bid+fresh_books[t].best_ask)/2*q-c
                                for t,(q,c) in old_basis.items() if q),D('0'))
                mark_at = min(fresh_books[t].observed_at for t,(q,c) in old_basis.items() if q)
            self.sdk.journal.append('day_baseline',dict(day=day.isoformat(),at=now,inventory_pnl=baseline,
                mark_at=mark_at,source='observed_boundary' if mark_at is not None else 'flat' if baseline is not None else UNKNOWN))
        else:
            baseline = baselines[-1]['inventory_pnl']
        samples = [p for _,p in self.sdk.journal.events('occupation') if p['at'].astimezone(UTC).date() == day and p['at'] <= now]
        average = None
        if samples:
            samples.sort(key=lambda p:p['at'])
            duration = D(str((now-samples[0]['at']).total_seconds()))
            integral = D('0')
            for index,p in enumerate(samples):
                end = samples[index+1]['at'] if index+1 < len(samples) else now
                integral += p['total']*D(str((end-p['at']).total_seconds()))
            if duration > 0 and integral > 0:
                average = integral/duration
        reward,rebate = income['reward_today'],income['rebate_today']
        if average is not None and isinstance(reward,D):
            metrics['lp_yield_pct_per_day'] = reward/average*100
        if all(isinstance(v,D) for v in (reward,rebate,baseline,inventory_pnl)):
            net = reward+rebate+paired+inventory_pnl-baseline-fees
            metrics['net_profit'] = net
            if average is not None:
                metrics['net_yield_pct_per_day'] = net/average*100
        self._valuation_at = now if inventory_pnl is not None else None
        return metrics

    def _cancel_owned(self, elapsed, attempted, *, at=None, active=()):
        for oid in sorted(set(self.sdk.known_open_order_ids()) | set(active)):
            if elapsed-attempted.get(oid,float('-inf')) < 5:
                continue
            attempted[oid]=elapsed
            try:
                self.sdk.cancel_order(oid,at=at if at is not None else self.clock())
            except Exception:
                self.sdk.journal.append('diagnostic',dict(at=self.clock(),reason='owned cancel unavailable'))

    def drain(self, *, timeout_s=90, monotonic=None, wait=None):
        from time import monotonic as real_monotonic, sleep
        from lp_v2.models import OrderStatus
        monotonic = monotonic or real_monotonic
        wait = wait or sleep
        self.stop.set()
        deadline, attempted = monotonic()+timeout_s, {}
        code = 1
        reconciled = False
        while True:
            with self.sdk.read_cycle(now=self.clock()):
                # Cancellation of proven own ids must not depend on healthy read endpoints.
                self._cancel_owned(monotonic(),attempted)
                owned = set(self.sdk.known_order_ids())
                active, opens_verified = set(), False
                try:
                    for page in self.sdk.client.list_open_orders():
                        for order in page.items:
                            if not isinstance(order.id,str) or not order.id:
                                raise ValueError('incomplete open order readback')
                            if order.id in owned:
                                active.add(order.id)
                    opens_verified = True
                except Exception:
                    pass
                # Current own presence overrides stale terminal history, even if other reads fail.
                self._cancel_owned(monotonic(),attempted,active=active)
                try:
                    self._reconcile()
                    if not opens_verified:
                        raise ValueError('incomplete open order readback')
                    reconciled = True
                    self._accounting_at = self.clock()
                    state = self.ledger.snapshot()
                    possible = {o.order_id for o in state.orders if not o.authoritative or o.status == OrderStatus.LIVE}
                    self._cancel_owned(monotonic(),attempted,active=active)
                    if not active and not possible and not state.pending:
                        code = 0
                        break
                except Exception:
                    reconciled = False
                    self.sdk.journal.append('diagnostic',dict(at=self.clock(),reason='stop readback unavailable'))
            if monotonic() >= deadline:
                break
            wait(min(1,max(0,deadline-monotonic())))
        state = self.ledger.snapshot()
        self.sdk.journal.append('stop_result',dict(at=self.clock(),exit_code=code,
            orders=state.orders,pending=state.pending,positions=state.positions,merges=state.merges,
            inventory=self._audit(self.clock(),reconciled,self._last_plans)['inventory'],
            reconciliation='SUCCESS' if reconciled else UNKNOWN,accounting_as_of=self._accounting_at))
        self.exit_code = code
        with self._lock:
            self._snapshot = self._build(self.clock())
        return code

    def run(self):
        try:
            while not self.stop.is_set():
                self.step()
                self.stop.wait(1)
        finally:
            try:
                self.drain(timeout_s=self.stop_timeout_s)
            finally:
                self.sdk.close()
                if hasattr(self.observations,'close'):
                    self.observations.close()


class MarketCheckFailure(ValueError):
    """Only application-authored diagnostics, with a validated public identity."""
    def __init__(self, market):
        import re
        self.market = market if isinstance(market,str) and re.fullmatch('0x[0-9a-fA-F]{64}',market) else 'UNKNOWN'
        self.stage = 'market_check'
        self.reason_code = 'MARKET_VALIDATION_UNAVAILABLE'
        super().__init__(self.reason_code)


def create_live_runner(config, *, public=None, http=None, clock=None):
    import json
    from pathlib import Path
    from polymarket import SecureClient
    from lp_v2.relayer_credentials import parse_relayer_credentials
    from polymarket.models.clob.api_key import ApiKeyCreds
    from lp_v2.cli import owner_file
    from lp_v2.journal import Journal
    from lp_v2.live_sdk import LiveSDK
    from lp_v2.readonly_sdk import ReadOnlySDK
    if not config.relayer_credentials_file:
        raise ValueError('explicit relayer credential file required for Proxy merge')
    c=config.credentials
    try:
        from eth_account import Account
        from polymarket.environments import PRODUCTION
        from polymarket._internal.wallet import classify_wallet_type
        with owner_file(config.private_key_file) as source:
            key=source.read().strip()
        signer=Account.from_key(key).address
        if (type(c.signature_type) is not int or c.signature_type != 1
                or signer.lower() != c.signer_address.lower()
                or classify_wallet_type(signer=signer,wallet=c.wallet_address,config=PRODUCTION.wallet_derivation) != 'POLY_PROXY'):
            raise ValueError('identity mismatch')
    except Exception:
        raise ValueError('signing and L2/Proxy identity mismatch') from None
    clock = clock or (lambda: datetime.now(UTC))
    observations=ReadOnlySDK(public=public,http=http,credentials=c,clock=clock)
    client=journal=scanner=None
    try:
        try:
            rows=observations.read_fixed_market(config.started_at,config.market)
        except Exception:
            raise MarketCheckFailure(config.market) from None
        tokens=tuple(t.token_id for t in rows[0]['plan'].market.tokens)
        with owner_file(config.relayer_credentials_file) as source:
            try:
                relayer_payload=json.load(source)
                relayer_key=parse_relayer_credentials(relayer_payload,signer)
            except (ValueError,TypeError):
                raise ValueError('invalid relayer credentials') from None
        client=SecureClient.create(private_key=key,wallet=c.wallet_address,
            credentials=ApiKeyCreds(key=c.key,secret=c.secret,passphrase=c.passphrase),api_key=relayer_key)
        key=None
        if (client.wallet_type != 'POLY_PROXY' or str(client.wallet).lower() != c.wallet_address.lower()
                or str(client.signer).lower() != c.signer_address.lower()):
            raise ValueError('signing and L2 identities disagree')
        from lp_v2.rotation_state import carry_legacy_journal,open_market_journal,read_control_window
        action_id,window_end = read_control_window(config)
        identity=dict(owner='lp-v2:'+c.wallet_address.lower(),condition_id=config.market,token_ids=tokens)
        carry_legacy_journal(config.state_dir,identity)
        journal=open_market_journal(config.state_dir,identity)
        sdk=LiveSDK(client,journal,**identity,wallet=c.wallet_address,signer=c.signer_address,clock=clock)
        sdk.rpc,sdk.relayer=client._ctx.rpc,client._ctx.relayer
        import httpx
        sdk.http=httpx.Client(timeout=5,trust_env=False,follow_redirects=False)
        from lp_v2.live_control.profiles import read_json
        control_run_id=read_json(Path(config.state_dir)/'process.json')['run_id'] if action_id is not None else None
        from lp_v2.rotation_live import RotationRunner
        from lp_v2.rotation_scan import ScanWorker
        current = LiveRunner(config,sdk,observations,clock=clock)
        scanner = ScanWorker(observations.new_scan_sdk(),clock=clock)
        return RotationRunner(config,current,scanner,action_id=action_id,window_end=window_end,clock=clock,
            account_identity=dict(signer_address=c.signer_address,wallet_address=c.wallet_address,signature_type=c.signature_type),
            relayer=relayer_payload,control_run_id=control_run_id)
    except BaseException:
        if scanner is not None:
            scanner.stop(0)
            scanner.sdk.close()
        if journal is not None:
            journal.close()
        if client is not None:
            client.close()
        observations.close()
        raise
