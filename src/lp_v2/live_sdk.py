"""Fixed-market execution boundary; durable intents are never blindly retried."""
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from contextlib import contextmanager
from decimal import Decimal as D
from polymarket.models.clob.order_response import AcceptedOrder, RejectedOrder
from polymarket.errors import RequestRejectedError
from polymarket.clients._transport import _read_json
from polymarket._internal.actions.account import build_get_order_request, parse_open_order

from lp_v2.models import Balance, Fill, Position, MergeResult, Order, OrderResult, OrderStatus, Outcome


_UNAVAILABLE = object()


class _ReadbackError(ValueError):
    """Fixed application reason code; never carries exchange exception text."""


class LiveSDK:
    def __init__(self, client, journal, *, owner, condition_id, token_ids, wallet, signer, markets=None, clock=lambda: datetime.now(UTC)):
        self.client, self.journal = client, journal
        self.owner, self.condition_id, self.token_ids = owner, condition_id, tuple(token_ids)
        from types import MappingProxyType
        pairs = {cid:tuple(pair) for cid,pair in (markets or {condition_id:token_ids}).items()}
        if (not 1 <= len(pairs) <= 3 or pairs.get(condition_id) != tuple(token_ids)
                or any(len(pair) != 2 or not all(pair) or pair[0] == pair[1] for pair in pairs.values())
                or len({t for pair in pairs.values() for t in pair}) != 2*len(pairs)):
            raise ValueError('invalid frozen market identities')
        self.markets = MappingProxyType(pairs)
        identities=journal.events('identity')
        first=identities[0][1] if identities else {}
        self.legacy_condition_id=first.get('condition_id',next(iter(first.get('markets',pairs))))
        self.wallet, self.signer = wallet, signer
        self.rpc = self.relayer = self.http = None
        self._order_reads = None
        self._history_cache = None
        self.clock = clock
        self._cycle_at = None
        self.readback_blocker = None

    @contextmanager
    def read_cycle(self, *, now=None):
        """Reuse order reads inside one executor round; never across rounds."""
        if self._order_reads is not None:
            yield
            return
        self._order_reads = {}
        self._cycle_at = now
        try:
            yield
        finally:
            self._order_reads = None
            self._history_cache = None
            self._cycle_at = None

    def _now(self):
        return self._cycle_at if self._cycle_at is not None else self.clock()

    def _order_readback(self, order_id):
        cache = self._order_reads if self._order_reads is not None else {}
        if order_id not in cache:
            try:
                path, params = build_get_order_request(order_id=order_id)
                response = self.client._ctx.secure_clob._request('GET', path, params=params)
                raw = _read_json(response)
                cache[order_id] = None if response.status_code == 200 and raw is None else parse_open_order(raw)
            except RequestRejectedError as error:
                cache[order_id] = None if error.status == 404 else _UNAVAILABLE
            except Exception:
                cache[order_id] = _UNAVAILABLE
        return cache[order_id]

    def _validate(self, request, at):
        q, terms = request.quote, request.reservation_terms
        if (request.owner != self.owner or q.condition_id not in self.markets or q.token_id not in self.markets[q.condition_id]
                or terms is None or terms.token_ids != self.markets[q.condition_id] or not request.request_id
                or not q.price.is_finite() or not 0 < q.price < 1
                or not q.quantity.is_finite() or q.quantity <= 0
                or request.expires_at != request.created_at + timedelta(hours=1)
                or not request.created_at <= at < request.expires_at):
            raise ValueError('invalid fixed-market intent')

    def place_order(self, request, *, at):
        self._validate(request, at)
        existing = self._history()
        if request.request_id in existing:
            result = existing[request.request_id]
            if result.request != request:
                raise ValueError('request identity reused')
            return result if isinstance(result, OrderResult) else OrderResult(request, Outcome.SUCCESS, result, at)
        ceiling = D('.98') - request.quote.price - sum(request.reservation_terms.fee_per_share)
        if request.purpose == 'hedge':
            opposite = next((p for p in self.read_positions() if p.condition_id == request.quote.condition_id and p.token_id != request.quote.token_id and p.quantity > 0), None)
            if opposite is None:
                raise ValueError('hedge ceiling requires opposite cost proof')
            index = self.markets[request.quote.condition_id].index(request.quote.token_id)
            ceiling = D('.98') - opposite.cost_usdc/opposite.quantity - request.reservation_terms.fee_per_share[index]
            if request.quote.price > ceiling:
                raise ValueError('hedge exceeds cost ceiling')
        self.journal.append('intent', dict(request=request, hedge_ceiling=ceiling))
        q = request.quote
        order, outcome, diagnostic = None, Outcome.UNKNOWN, 'ambiguous submission'
        dispatched = False
        try:
            signed_at = self.clock()
            authorization = self._signature_authorization(signed_at)
            signed = self.client.create_limit_order(token_id=q.token_id, price=q.price, size=q.quantity,
                side='BUY', post_only=True, expiration=int(request.expires_at.timestamp()))
            if (signed.token_id != q.token_id or signed.side != 'BUY' or signed.post_only is not True
                    or signed.order_type != 'GTD' or signed.expiration != int(request.expires_at.timestamp())
                    or signed.maker.lower() != self.wallet.lower() or signed.signer.lower() != self.signer.lower()):
                raise ValueError('signed identity mismatch')
            signed_ids = _order_ids(signed)
            self.journal.append('signed', dict(request_id=request.request_id, salt=signed.salt, token_id=signed.token_id,
                maker=signed.maker, signer=signed.signer, maker_amount=signed.maker_amount,
                taker_amount=signed.taker_amount, expiration=signed.expiration, timestamp=signed.timestamp,
                order_ids=signed_ids, signed_at=signed_at, **authorization))
            dispatched = True
            response = self.client.post_order(signed)
            if isinstance(response, AcceptedOrder) and response.order_id:
                order = Order(response.order_id, request, D('0'), q.quantity, OrderStatus.LIVE, at, False)
                outcome, diagnostic = Outcome.SUCCESS, 'accepted awaiting readback'
                if response.order_id not in signed_ids:
                    self.journal.append('diagnostic',dict(request_id=request.request_id,order_id=response.order_id,reason='accepted order id mismatch',at=at))
            elif isinstance(response, RejectedOrder) and response.code != 'unknown':
                outcome, diagnostic = Outcome.FAILED, 'explicit rejection'
        except Exception:
            outcome = Outcome.UNKNOWN if dispatched else Outcome.FAILED
            diagnostic = 'submission unresolved' if dispatched else '未提交'
        result = OrderResult(request, outcome, order, at)
        self.journal.append('order_result', result)
        # Diagnostics are fixed application text; raw exceptions may contain credentials.
        if outcome != Outcome.SUCCESS:
            self.journal.append('diagnostic', dict(request_id=request.request_id, reason=diagnostic, at=at))
        return result

    def bind_entry(self, *, action_id, window_end, activation_id):
        if (not isinstance(action_id, str) or not action_id or not isinstance(activation_id, str) or not activation_id
                or not isinstance(window_end, datetime) or window_end.tzinfo is None):
            raise ValueError('unproven entry authorization')
        record = dict(action_id=action_id,window_end=window_end,activation_id=activation_id)
        previous = self.journal.events('entry_window')
        if not previous or previous[-1][1] != record:
            self.journal.append('entry_window',record)

    def _signature_authorization(self, now):
        windows = self.journal.events('entry_window')
        if not windows:
            return {}
        authorization = windows[-1][1]
        activated = any(p.get('activation_id') == authorization['activation_id'] for _,p in self.journal.events('signed'))
        if (not isinstance(now, datetime) or now.tzinfo is None
                or (not activated and now >= authorization['window_end'])):
            raise ValueError('entry authorization window expired')
        return dict(authorization, entry_signature=not activated)

    def _history(self):
        if self._order_reads is not None:
            revision = (self.journal.last_sequence('intent'),self.journal.last_sequence('order_result'))
            if self._history_cache is not None and self._history_cache[0] == revision:
                return self._history_cache[1].copy()
        requests = {p['request'].request_id: p['request'] for _, p in self.journal.events('intent')}
        results = {key: OrderResult(r, Outcome.UNKNOWN, None, r.created_at) for key, r in requests.items()}
        for _, result in self.journal.events('order_result'):
            results[result.request.request_id] = result
        if self._order_reads is not None:
            self._history_cache = revision,results.copy()
        return results

    def _observed_order(self, raw, oid, previous, cancels):
        r, q = previous.request, previous.request.quote
        if (raw.id != oid or raw.maker_address.lower() != self.wallet.lower()
                or raw.condition_id != q.condition_id or q.condition_id not in self.markets or raw.token_id != q.token_id
                or raw.side != 'BUY' or raw.price != q.price or raw.original_size != q.quantity
                or raw.order_type != 'GTD' or raw.expires_at != datetime.fromtimestamp(int(r.expires_at.timestamp()), UTC)):
            raise ValueError('owned order readback identity mismatch')
        uncertain = raw.status in ('DELAYED','UNMATCHED')
        status = {'LIVE': OrderStatus.LIVE, 'DELAYED': OrderStatus.LIVE, 'UNMATCHED': OrderStatus.LIVE,
                  'MATCHED': OrderStatus.FILLED, 'CANCELED': OrderStatus.CANCELLED,
                  'CANCELLED': OrderStatus.CANCELLED}.get(raw.status)
        if status is None or not D('0') <= raw.size_matched <= q.quantity:
            raise ValueError('unknown order status or quantity')
        prior = previous.order.filled_quantity if previous.order else D('0')
        if status != OrderStatus.LIVE and raw.size_matched < prior:
            raise ValueError('terminal cumulative quantity regressed')
        filled = max(raw.size_matched,prior) if status == OrderStatus.LIVE else raw.size_matched
        observed_at = self._now()
        order = Order(oid,r,filled,q.quantity-filled if status == OrderStatus.LIVE else D('0'),
                      status,observed_at,not uncertain,cancels.get(oid))
        return OrderResult(r,Outcome.UNKNOWN if uncertain else Outcome.SUCCESS,order,observed_at)

    def _missing_order(self, previous, candidates, cancels):
        # Iterator exhaustion is required: a later-page failure cannot prove absence.
        try:
            opens = tuple(o for page in self.client.list_open_orders(market=previous.request.quote.condition_id) for o in page.items)
            if any(not isinstance(o.id,str) or not o.id for o in opens):
                raise ValueError('incomplete open order readback')
        except Exception:
            raise _ReadbackError('open_orders_unavailable') from None
        try:
            trades = self._trade_facts()
        except Exception:
            raise _ReadbackError('trade_history_unavailable') from None
        recorded = tuple(f for _,f in self.journal.events('fill') if f.request_id == previous.request.request_id)
        owned = {f.order_id for f in (*trades.values(),*recorded)
                 if f.request_id == previous.request.request_id}
        rows = [o for o in opens if o.id in candidates]
        identities = owned | {o.id for o in rows}
        if len(identities) > 1 or len(rows) > 1:
            raise ValueError('conflicting signed order identities')
        if rows:
            return self._observed_order(rows[0],rows[0].id,previous,cancels)
        if previous.order or owned:
            oid = previous.order.order_id if previous.order else next(iter(owned))
            filled = sum((f.quantity for f in trades.values() if f.order_id == oid),D('0'))
            proven = max(previous.order.filled_quantity if previous.order else D('0'),
                         sum((f.quantity for _,f in self.journal.events('fill') if f.order_id == oid),D('0')))
            if filled < proven or any(f.trade_id not in trades for f in recorded):
                raise _ReadbackError('trade_history_mismatch')
            quantity = previous.request.quote.quantity
            cancel_at = cancels.get(oid) or (previous.order.cancel_requested_at if previous.order else None)
            terminal = (cancel_at is not None or self._now() >= previous.request.expires_at+timedelta(seconds=120)
                        or filled == quantity)
            status = (OrderStatus.FILLED if filled == quantity else OrderStatus.CANCELLED) if terminal else OrderStatus.LIVE
            order = Order(oid,previous.request,filled,D('0') if terminal else quantity-filled,
                          status,self._now(),terminal,cancel_at)
            return OrderResult(previous.request,Outcome.SUCCESS if terminal else Outcome.UNKNOWN,order,self._now())
        if len(candidates) == 2 and self._now() >= previous.request.expires_at+timedelta(seconds=120):
            return OrderResult(previous.request,Outcome.FAILED,None,self._now())
        return previous

    def read_orders(self):
        self.readback_blocker = None
        results = self._history()
        signed = {p['request_id']:p for _,p in self.journal.events('signed')}
        cancels = {p['order_id']:p['at'] for _,p in self.journal.events('cancel_intent')}
        trades = None
        for key,previous in results.items():
            if previous.outcome == Outcome.FAILED and previous.order is None:
                continue
            if previous.order is not None and previous.order.authoritative and previous.order.status != OrderStatus.LIVE:
                # A cancellation snapshot can precede delivery of an authenticated
                # trade. Refresh only when complete trade provenance proves growth;
                # ordinary terminal history still needs no repeated order reads.
                if trades is None:
                    trades = self._trade_facts()
                total = sum((f.quantity for f in trades.values() if f.request_id == key),D('0'))
                if total <= previous.order.filled_quantity:
                    continue
                raw = self._order_readback(previous.order.order_id)
                if raw is None or raw is _UNAVAILABLE:
                    raise ValueError('late fill requires authoritative order readback')
                updated = self._observed_order(raw,previous.order.order_id,previous,cancels)
                if (not updated.order.authoritative or updated.order.filled_quantity != total
                        or updated.order.status == OrderStatus.LIVE):
                    raise ValueError('late fill does not reconcile with terminal history')
                self.journal.append('order_result',updated)
                self.journal.append('late_fill_reconciliation',dict(request_id=key,previous=previous,
                    current=updated,trade_ids=tuple(f.trade_id for f in trades.values() if f.request_id == key),at=self._now()))
                results[key] = updated
                continue
            candidates = (previous.order.order_id,) if previous.order else signed.get(key,{}).get('order_ids',())
            found, missing = [], []
            for oid in candidates:
                raw = self._order_readback(oid)
                if raw is None:
                    missing.append(oid)
                elif raw is _UNAVAILABLE:
                    if self.readback_blocker is None:
                        self.readback_blocker = dict(code='order_readback_unavailable', order_tail=oid[-8:])
                else:
                    found.append(self._observed_order(raw,oid,previous,cancels))
            if len(found) > 1:
                raise ValueError('conflicting signed order identities')
            result = found[0] if found else previous
            if not found and candidates and len(missing) == len(candidates):
                try:
                    result = self._missing_order(previous,candidates,cancels)
                except Exception as error:
                    if self.readback_blocker is None:
                        code = str(error) if isinstance(error, _ReadbackError) else 'order_readback_unavailable'
                        self.readback_blocker = dict(code=code, order_tail=candidates[0][-8:])
                    result = previous
            if result is previous and previous.order is not None:
                result = OrderResult(previous.request,Outcome.UNKNOWN,replace(previous.order,authoritative=False),previous.observed_at)
            # Observation freshness is returned, while the journal records fact transitions.
            comparable = replace(result,observed_at=previous.observed_at,
                order=replace(result.order,observed_at=previous.order.observed_at)
                if result.order is not None and previous.order is not None else result.order)
            if comparable != previous:
                self.journal.append('order_result',result)
                if result.outcome == Outcome.FAILED and result.order is None:
                    reason = '到期未见'
                elif result.outcome == Outcome.UNKNOWN:
                    reason = 'order readback unresolved'
                else:
                    reason = None
                if reason:
                    self.journal.append('diagnostic',dict(request_id=key,reason=reason,at=self._now()))
            results[key] = result
        return tuple(r.order if r.order else r for r in results.values())

    def orders_verified(self):
        return all((r.outcome == Outcome.FAILED and r.order is None)
                   or (r.order is not None and r.order.authoritative) for r in self._history().values())

    def known_order_ids(self):
        return tuple(sorted(r.order.order_id for r in self._history().values() if r.order is not None))

    def known_open_order_ids(self):
        """Durable ownership only; this read never depends on exchange availability."""
        return tuple(sorted(r.order.order_id for r in self._history().values() if r.order is not None
                            and (not r.order.authoritative or r.order.status == OrderStatus.LIVE)))

    def cancel_order(self, order_id, *, at):
        known = next((r for r in self._history().values() if r.order and r.order.order_id == order_id), None)
        if known is None or known.request.quote.condition_id not in self.markets or known.request.quote.token_id not in self.markets[known.request.quote.condition_id]:
            raise ValueError('cannot cancel foreign or unknown order')
        cancelled = known.order.cancel_requested_at is not None or any(
            p['order_id'] == order_id for _, p in self.journal.events('cancel_intent'))
        if cancelled and at >= known.request.expires_at + timedelta(seconds=120):
            try:
                opens = tuple(o for page in self.client.list_open_orders(market=known.request.quote.condition_id)
                              for o in page.items)
                if all(isinstance(o.id, str) and o.id for o in opens) and all(o.id != order_id for o in opens):
                    return known
            except Exception:
                pass
        self.journal.append('cancel_intent', dict(order_id=order_id, at=at))
        outcome = Outcome.UNKNOWN
        try:
            raw = self.client.cancel_order(order_id=order_id)
            if order_id in raw.canceled:
                outcome = Outcome.SUCCESS
            elif order_id in raw.not_canceled:
                outcome = Outcome.FAILED
        except Exception:
            pass
        if self._order_reads is not None:
            self._order_reads.pop(order_id,None)
        order = replace(known.order, authoritative=False, observed_at=at, cancel_requested_at=at)
        result = OrderResult(known.request, outcome, order, at)
        self.journal.append('order_result', result)
        self.journal.append('diagnostic', dict(order_id=order_id, reason='cancel '+outcome.value, at=at))
        return result

    def _trade_facts(self):
        results = self._history()
        signed = {p['request_id']:p for _,p in self.journal.events('signed')}
        orders = {}
        for key, result in results.items():
            ids = ((result.order.order_id,) if result.order else
                   signed.get(key,{}).get('order_ids',()) if result.outcome != Outcome.FAILED else ())
            for oid in ids:
                if oid in orders and orders[oid] != result.request:
                    raise ValueError('conflicting owned order identity')
                orders[oid] = result.request
        facts = {}
        known = {f.trade_id:f for _,f in self.journal.events('fill')}
        identities = {f.request_id:f.order_id for _,f in self.journal.events('fill')}
        identities.update({key:r.order.order_id for key,r in results.items() if r.order})
        for cid in self.markets:
            for page in self.client.list_account_trades(market=cid, maker_address=self.wallet):
                for raw in page.items:
                    for maker in raw.maker_orders:
                        if maker.order_id not in orders:
                            continue
                        r = orders[maker.order_id]
                        if (raw.condition_id != cid or raw.condition_id != r.quote.condition_id or maker.token_id != r.quote.token_id
                                or maker.maker_address.lower() != self.wallet.lower() or maker.side != 'BUY'
                                or maker.fee_rate_bps != 0):
                            raise ValueError('fill provenance or fee unavailable')
                        if raw.status == 'FAILED':
                            continue
                        if raw.status != 'CONFIRMED':
                            raise ValueError('owned fill is not settled')
                        if (not maker.matched_amount.is_finite() or not 0 < maker.matched_amount <= r.quote.quantity
                                or not maker.price.is_finite() or not 0 < maker.price <= r.quote.price):
                            raise ValueError('invalid owned fill quantity or price')
                        if r.request_id in identities and identities[r.request_id] != maker.order_id:
                            raise ValueError('conflicting signed order identities')
                        identities[r.request_id] = maker.order_id
                        fill = Fill(raw.id+':'+maker.order_id, maker.order_id, r.request_id, self.owner,
                            cid, maker.token_id, maker.matched_amount, maker.price, D('0'), raw.matched_at)
                        if ((fill.trade_id in facts and facts[fill.trade_id] != fill)
                                or (fill.trade_id in known and known[fill.trade_id] != fill)):
                            raise ValueError('fill identity changed')
                        facts[fill.trade_id] = fill
        for oid,r in orders.items():
            if sum((f.quantity for f in facts.values() if f.order_id == oid),D('0')) > r.quote.quantity:
                raise ValueError('owned fills exceed intent')
        return facts

    def read_trades(self):
        facts = self._trade_facts()
        known = {f.trade_id:f for _,f in self.journal.events('fill')}
        for key,fill in facts.items():
            if key in known and known[key] != fill:
                raise ValueError('fill identity changed')
        for key,fill in facts.items():
            if key not in known:
                self.journal.append('fill',fill)
                known[key] = fill
        return tuple(known.values())

    def _positions(self):
        positions = {}
        events = [(f.executed_at,0,f.trade_id,f) for _,f in self.journal.events('fill')]
        events += [(r.observed_at,1,r.request.merge_id,r) for r in self._merge_results().values() if r.received_usdc]
        for _,_,_,fact in sorted(events):
            if isinstance(fact, Fill):
                previous = positions.get(fact.token_id, Position(self.owner,fact.condition_id,fact.token_id,D('0'),D('0'),fact.executed_at))
                positions[fact.token_id] = replace(previous, quantity=previous.quantity+fact.quantity,
                    cost_usdc=previous.cost_usdc+fact.quantity*fact.price+fact.fee_usdc, updated_at=max(previous.updated_at,fact.executed_at))
            else:
                for consumed in fact.consumed_positions:
                    previous = positions[consumed.token_id]
                    positions[consumed.token_id] = replace(previous,quantity=previous.quantity-consumed.quantity,
                        cost_usdc=previous.cost_usdc-consumed.cost_usdc,updated_at=max(previous.updated_at,fact.observed_at))
        return positions

    def read_positions(self):
        self.read_trades()
        self._refresh_merges()
        positions = self._positions()
        for token in (t for pair in self.markets.values() for t in pair):
            raw = self.client.get_balance_allowance(asset_type='CONDITIONAL', token_id=token)
            actual = D(raw.balance)/1000000
            expected = positions[token].quantity if token in positions else D('0')
            if actual != expected:
                raise ValueError('position readback does not match owned trade proof')
        return tuple(positions.values())

    def _cash(self):
        return D(self.client.get_balance_allowance(asset_type='COLLATERAL').balance)/1000000

    def read_balance(self):
        self._refresh_merges()
        return Balance(self._cash(), datetime.now(UTC), tuple(self._merge_results().values()))

    def _merge_results(self):
        results = {p['request'].merge_id: MergeResult(p['request'],Outcome.UNKNOWN,D('0'),p['request'].created_at)
                   for _,p in self.journal.events('merge_intent')}
        for _,r in self.journal.events('merge_result'):
            results[r.request.merge_id] = r
        return results

    def merge(self, request, *, at):
        existing = self._merge_results().get(request.merge_id)
        if existing is not None:
            if existing.request != request:
                raise ValueError('merge identity reused')
            return existing
        if (request.owner != self.owner or request.condition_id not in self.markets
                or (request.yes_token_id,request.no_token_id) != self.markets[request.condition_id]
                or not request.quantity.is_finite() or request.quantity <= 0
                or request.quantity*1000000 != int(request.quantity*1000000)):
            raise ValueError('invalid fixed-market merge')
        positions = {p.token_id:p for p in self.read_positions()}
        if any(token not in positions or positions[token].quantity < request.quantity for token in self.markets[request.condition_id]):
            raise ValueError('merge lacks paired owned inventory')
        self.journal.append('merge_intent',dict(request=request,cash=self._cash(),positions=tuple(positions.values())))
        try:
            handle = self.client.merge_positions(condition_id=request.condition_id,amount=int(request.quantity*1000000),metadata=request.merge_id)
            from polymarket.transactions import SyncGaslessTransactionHandle
            if not isinstance(handle,SyncGaslessTransactionHandle):
                raise ValueError('only Proxy gasless merge is supported')
            self.journal.append('merge_handle',dict(merge_id=request.merge_id,transaction_id=handle.transaction_id,transaction_hash=handle.transaction_hash))
        except Exception:
            self.journal.append('diagnostic',dict(at=at,merge_id=request.merge_id,reason='merge unresolved'))
        return self._merge_results()[request.merge_id]

    def _refresh_merges(self):
        if self.rpc is None or self.relayer is None:
            return
        from polymarket._internal.actions.relayer.poll import fetch_gasless_transaction_sync
        handles = {p['merge_id']:p for _,p in self.journal.events('merge_handle')}
        for _,baseline in self.journal.events('merge_intent'):
            r = baseline['request']
            previous = self._merge_results()[r.merge_id]
            if previous.received_usdc or previous.outcome == Outcome.FAILED or r.merge_id not in handles:
                continue
            try:
                handle = handles[r.merge_id]
                tx = fetch_gasless_transaction_sync(self.relayer,transaction_id=handle['transaction_id'])
                if tx.transaction_id != handle['transaction_id']:
                    continue
                if tx.state in ('STATE_FAILED','STATE_INVALID'):
                    self.journal.append('merge_result',MergeResult(r,Outcome.FAILED,D('0'),datetime.now(UTC)))
                    continue
                txhash = tx.transaction_hash
                if tx.state != 'STATE_CONFIRMED' or not txhash or (handle['transaction_hash'] and handle['transaction_hash'] != txhash):
                    continue
                receipt = self.rpc.eth_get_transaction_receipt(txhash)
                if not receipt or receipt.get('transactionHash') != txhash:
                    continue
                at = datetime.now(UTC)
                if receipt.get('status') == '0x0':
                    self.journal.append('merge_result',MergeResult(r,Outcome.FAILED,D('0'),at))
                    continue
                if receipt.get('status') != '0x1' or not _receipt_proves_merge(receipt,r,self.wallet):
                    continue
                before = {p.token_id:p for p in baseline['positions']}
                consumed = tuple(replace(before[t],quantity=r.quantity,
                    cost_usdc=before[t].cost_usdc if r.quantity == before[t].quantity else before[t].cost_usdc*r.quantity/before[t].quantity,
                    updated_at=at) for t in self.markets[r.condition_id])
                self.journal.append('merge_receipt',dict(merge_id=r.merge_id,receipt=receipt,at=at))
                self.journal.append('merge_result',MergeResult(r,Outcome.SUCCESS,r.quantity,at,consumed))
            except Exception:
                # Read failures leave the original attempt and full reservation intact.
                continue

    def read_income(self, now, condition_id=None):
        condition_id = condition_id or self.condition_id
        if condition_id not in self.markets:
            raise ValueError('income market not selected')
        from polymarket.environments import PRODUCTION
        day = now.astimezone(UTC).date()
        previous = self.journal.latest_market_event('income',condition_id,self.legacy_condition_id)
        if previous is not None:
            if previous['at'].astimezone(UTC).date() == day and 0 <= (now-previous['at']).total_seconds() < 60:
                return previous
        result = dict(at=now,condition_id=condition_id,reward_today='UNKNOWN',rebate_today='UNKNOWN',reward_share='UNKNOWN',scoring='UNKNOWN')
        assets = {PRODUCTION.collateral_token.lower(),'0x2791bca1f2de4661ed88a30c99a7a9449aa84174'}
        def amount(value):
            if isinstance(value,bool):
                raise ValueError('invalid amount')
            number = D(str(value))
            if not number.is_finite() or number < 0:
                raise ValueError('invalid amount')
            return number
        try:
            scoring = {}
            for r in self._history().values():
                if r.request.quote.condition_id == condition_id and r.order is not None and r.order.authoritative and r.order.status == OrderStatus.LIVE:
                    flag = self.client.get_order_scoring(order_id=r.order.order_id)
                    if type(flag) is not bool:
                        raise ValueError('invalid scoring fact')
                    scoring[r.order.order_id] = flag
            result['scoring'] = scoring
        except Exception:
            pass
        try:
            total = D('0')
            for page in self.client.list_user_earnings_for_day(date=day.isoformat()):
                for row in page.items:
                    if row.condition_id != condition_id:
                        continue
                    if (row.date.astimezone(UTC).date() != day or row.maker_address.lower() != self.wallet.lower()
                            or row.asset_address.lower() not in assets):
                        raise ValueError('earning identity mismatch')
                    total += amount(row.earnings)*amount(row.asset_rate)
            result['reward_today'] = total
        except Exception:
            pass
        try:
            share = amount(self.client.get_reward_percentages()[condition_id])
            if share > 100:
                raise ValueError('invalid reward share')
            result['reward_share'] = share
        except Exception:
            pass
        try:
            response = self.http.get('https://clob.polymarket.com/rebates/current',params=dict(date=day.isoformat(),maker_address=self.wallet))
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows,list):
                raise ValueError('incomplete rebate readback')
            total = D('0')
            for row in rows:
                if row['condition_id'] != condition_id:
                    continue
                if (row['date'] != day.isoformat() or row['maker_address'].lower() != self.wallet.lower()
                        or row['asset_address'].lower() not in assets):
                    raise ValueError('rebate identity mismatch')
                total += amount(row['rebated_fees_usdc'])
            result['rebate_today'] = total
        except Exception:
            pass
        self.journal.append('income',result)
        return result

    def close(self):
        try:
            self.client.close()
        finally:
            if self.http is not None:
                self.http.close()
            self.journal.close()


def _order_ids(signed):
    # Pinned SDK does not expose its signed-order hash; use its own EIP712 encoding.
    from dataclasses import asdict
    from eth_utils import keccak
    from polymarket.environments import PRODUCTION
    from polymarket._internal.actions.orders.types import UnsignedOrder
    from polymarket._internal.actions.orders.typed_data import _app_domain_separator, _order_contents_hash
    payload = asdict(signed)
    payload.pop('signature')
    payload.pop('post_only')
    result = []
    for address in (PRODUCTION.standard_exchange, PRODUCTION.neg_risk_exchange):
        unsigned = UnsignedOrder(**payload, chain_id=PRODUCTION.chain_id, exchange_address=address)
        result.append('0x' + keccak(b'\x19\x01' + bytes.fromhex(_app_domain_separator(unsigned, protocol_version="2")[2:]) + _order_contents_hash(unsigned)).hex())
    return tuple(result)


def _receipt_proves_merge(receipt, request, wallet):
    from eth_abi import decode
    from eth_utils import keccak
    from polymarket.environments import PRODUCTION as env
    cash, burned = 0, {int(t):0 for t in (request.yes_token_id,request.no_token_id)}
    topic = lambda value: '0x'+value[2:].lower().zfill(64)
    transfer = '0x'+keccak(text='Transfer(address,address,uint256)').hex()
    single = '0x'+keccak(text='TransferSingle(address,address,address,uint256,uint256)').hex()
    batch = '0x'+keccak(text='TransferBatch(address,address,address,uint256[],uint256[])').hex()
    for log in receipt['logs']:
        address, topics = log['address'].lower(), [t.lower() for t in log['topics']]
        data = bytes.fromhex(log['data'][2:])
        if (address == env.collateral_token.lower() and len(topics) == 3 and topics[0] == transfer
                and topics[2] == topic(wallet)):
            cash += decode(['uint256'],data)[0]
        if (address in (env.conditional_tokens.lower(),env.position_manager.lower()) and len(topics) == 4
                and topics[2] == topic(wallet)):
            if topics[0] == single:
                token,quantity = decode(['uint256','uint256'],data)
                if token in burned:
                    burned[token] += quantity
            elif topics[0] == batch:
                ids, amounts = decode(['uint256[]','uint256[]'],data)
                if len(ids) != len(amounts):
                    return False
                for token,quantity in zip(ids,amounts):
                    if token in burned:
                        burned[token] += quantity
    amount = int(request.quantity*1000000)
    return cash == amount and all(q == amount for q in burned.values())
