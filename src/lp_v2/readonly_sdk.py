"""Read-only exchange boundary. No signing client or order execution capability."""
from typing import Protocol
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, repr=False)
class L2Credentials:
    key: str
    secret: str
    passphrase: str
    signer_address: str
    wallet_address: str
    signature_type: int

    def __repr__(self):
        return "L2Credentials(<redacted>)"


UNKNOWN = 'UNKNOWN'


class ConditionalIdentityUnavailable(ValueError):
    """Authored observational identity failure, distinct from a balance failure."""


class _Excluded(ValueError):
    """Only application-authored, identity-free reasons may reach the summary."""



class ReadOnlyPort(Protocol):
    def read_markets(self, now): ...
    def read_wallet(self, now): ...
    def read_orders(self, now): ...
    def read_region(self): ...
    def safety(self, since): ...
    def transport_safety(self): ...
    def close(self): ...


class ReadOnlySDK:
    def __init__(self, *, public=None, http=None, credentials=None, stop=None):
        from polymarket import PublicClient
        import httpx
        self.__public = public if public is not None else PublicClient()
        self.__http = http if http is not None else httpx.Client(timeout=5, follow_redirects=False, trust_env=False)
        self.__credentials = credentials
        self.__stop = stop
        from lp_v2.readonly_transport import ReadOnlyTransportAudit
        self.__audit = ReadOnlyTransportAudit(self.__public, self.__http)

    def read_wallet(self, now):
        from decimal import Decimal
        from polymarket.models.clob.account import BalanceAllowance
        try:
            data = self.__account_get('/balance-allowance', now,
                dict(asset_type='COLLATERAL', signature_type=str(self.__credentials.signature_type)))
            balance = BalanceAllowance.model_validate(data).balance
            if balance < 0:
                raise ValueError('balance')
            return dict(available_usdc=str(Decimal(balance) / Decimal('1000000')), as_of=now)
        except Exception:
            return dict(available_usdc=UNKNOWN, as_of=None)

    def safety(self, since):
        return self.__audit.safety(since)

    def transport_safety(self):
        return self.__audit.snapshot()

    def close(self):
        self.__public.close()
        self.__http.close()

    def read_markets(self, now):
        """Bounded political catalog scan; retain every discovered exclusion."""
        self.__audit.require_ready()
        from itertools import islice
        rows = []
        for page in islice(self.__public.list_markets(closed=False, tag_id=2, include_tag=True, page_size=20), 5):
            if self.__stop is not None and self.__stop.is_set():
                raise InterruptedError('stopping')
            for market in page.items:
                if self.__stop is not None and self.__stop.is_set():
                    raise InterruptedError('stopping')
                row = dict(title=market.question or UNKNOWN, condition_id=str(market.condition_id or UNKNOWN),
                           score=UNKNOWN, eligible=False, skip_reason='市场安全数据缺失或无效', plan=None)
                try:
                    from lp_v2.scoring import score_market
                    metadata = self.__metadata_market(market, now)
                    row['score'] = str(score_market(metadata).score)
                    row.update(self.__map_market(market, now, metadata))
                except _Excluded as error:
                    row['skip_reason'] = str(error)
                except Exception:
                    # Never propagate raw transport exceptions/account identity to logs.
                    pass
                rows.append(row)
        return rows

    def __metadata_market(self, raw, now, *, recovery=False, fee_wire=None):
        """Score facts are independent of whether a proposal can safely exist."""
        from decimal import Decimal as D
        from lp_v2.models import Market, Token
        import re
        cid = str(raw.condition_id)
        if not re.fullmatch('0x[0-9a-fA-F]{64}', cid):
            raise _Excluded('市场身份缺失或无效')
        tokens = (raw.outcomes.yes.token_id, raw.outcomes.no.token_id)
        if not all(tokens) or tokens[0] == tokens[1]:
            raise _Excluded('二元市场 token 身份缺失或无效')
        def number(value, positive=False):
            v = D(str(value))
            if not v.is_finite() or v < 0 or (positive and v == 0):
                raise ValueError('number')
            return v
        t, rw = raw.trading, raw.rewards
        # No verified fee model => UNKNOWN score, never optimistic zero fees.
        from lp_v2.fee_policy import wire_maker_policy
        enabled, taker_bps, rebate = wire_maker_policy(self.__audit.fee_wire(cid) if fee_wire is None else fee_wire)
        rates = [number(r.rewards_daily_rate) for r in (rw.clob_rewards or ())
                 if str(r.condition_id) == cid and r.asset_address.lower() == '0x2791bca1f2de4661ed88a30c99a7a9449aa84174'
                 and r.start_date <= now.date() and (r.end_date is None or r.end_date >= now.date())]
        if not rates and not recovery:
            raise _Excluded('缺少有效奖励输入')
        bid, ask = number(raw.prices.best_bid, True), number(raw.prices.best_ask, True)
        if not 0 < bid <= ask < 1:
            raise _Excluded('市场价格缺失或无效')
        return Market(cid, (Token(str(tokens[0]), 'Yes'), Token(str(tokens[1]), 'No')),
            number(t.minimum_tick_size, True), number(t.minimum_order_size, True), number(rw.rewards_min_size, True) if not recovery or rw.rewards_min_size else number(t.minimum_order_size, True),
            float(bid), float(ask), float(number(raw.metrics.liquidity_num)), float(number(raw.metrics.volume_24hr)),
            float(sum(rates)), float(number(rw.rewards_max_spread, True)), float(rebate), float(taker_bps), enabled, bool(rates),
            any(tag.slug == 'politics' for tag in raw.tags), None, raw.state.accepting_orders)

    def __map_market(self, raw, now, m, *, recovery=False):
        from dataclasses import replace
        from decimal import Decimal as D
        from lp_v2.models import Book, MarketPlan
        from lp_v2.scoring import eligible_market, score_market
        cid, st = m.condition_id, raw.state
        if not (st.active is True and st.closed is False and st.accepting_orders is True
                and st.enable_order_book is True and m.political is True):
            raise _Excluded('市场状态或政治分类缺失或不合格')
        midpoint = (D(str(m.best_bid)) + D(str(m.best_ask))) / 2
        if not recovery and not D('0.10') <= midpoint <= D('0.90'):
            raise _Excluded(f'中间价 {midpoint} 超出 0.10–0.90')
        if not recovery and m.liquidity_num < 1000:
            raise _Excluded('流动性低于 1000')
        # A successful CLOB object may omit in_play; explicit non-false remains excluded.
        response = self.__http.get('https://clob.polymarket.com/markets/' + cid)
        response.raise_for_status()
        state=response.json()
        if not isinstance(state,dict) or ('in_play' in state and state['in_play'] is not False):
            raise _Excluded('进行中状态未验证')
        books = []
        for token in m.tokens:
            if self.__stop is not None and self.__stop.is_set():
                raise InterruptedError('stopping')
            b = self.__public.get_order_book(token_id=token.token_id)
            if (str(b.condition_id) != cid or str(b.token_id) != token.token_id
                    or b.tick_size != m.tick_size or b.min_order_size != m.min_order_size or not b.bids or not b.asks
                    or b.timestamp is None or not 0 <= (now-b.timestamp).total_seconds() <= 60):
                raise _Excluded('订单簿身份、约束或时效未验证')
            bid = max(level.price for level in b.bids)
            ask = min(level.price for level in b.asks)
            if not bid.is_finite() or not ask.is_finite() or not 0 < bid < ask < 1:
                raise _Excluded('订单簿价格无效')
            books.append(Book(cid, token.token_id, bid, ask, b.timestamp))
        m = replace(m, best_bid=float(books[0].best_bid), best_ask=float(books[0].best_ask), in_play=False)
        eligible = eligible_market(m)
        midpoint = (books[0].best_bid + books[0].best_ask) / 2
        return dict(score=str(score_market(m).score), eligible=eligible,
                    skip_reason=None if eligible else f'中间价 {midpoint} 或流动性不符合条件',
                    plan=MarketPlan(m, tuple(books), max(m.min_order_size, m.rewards_min_size), (D('0'), D('0'))) if eligible or recovery else None)

    def __account_get(self, path, now, params):
        self.__audit.require_ready()
        import base64, hashlib, hmac, re
        c = self.__credentials
        if (path not in ('/balance-allowance', '/data/orders', '/auth/ban-status/closed-only') or c is None
                or not all(isinstance(v, str) and v for v in (c.key, c.secret, c.passphrase))
                or not all(re.fullmatch('0x[0-9a-fA-F]{40}', v) for v in (c.signer_address, c.wallet_address))
                or type(c.signature_type) is not int or c.signature_type not in (0, 1, 2)):
            raise ValueError('account read unavailable')
        timestamp = str(int(now.timestamp()))
        secret = base64.b64decode(c.secret + '=' * (-len(c.secret) % 4), altchars=b'-_', validate=True)
        if not secret:
            raise ValueError('account read unavailable')
        signature = base64.urlsafe_b64encode(hmac.new(secret, (timestamp+'GET'+path).encode(), hashlib.sha256).digest()).decode()
        response = self.__http.get('https://clob.polymarket.com' + path, params=params,
            headers=dict(POLY_ADDRESS=c.signer_address, POLY_API_KEY=c.key,
                         POLY_PASSPHRASE=c.passphrase, POLY_TIMESTAMP=timestamp, POLY_SIGNATURE=signature))
        response.raise_for_status()
        return response.json()

    def read_orders(self, now):
        # Readback is observational: old wallet orders never enter our ledger.
        try:
            count, cursor, seen = 0, None, set()
            for _ in range(5):
                data = self.__account_get('/data/orders', now, {} if cursor is None else dict(next_cursor=cursor))
                if (not isinstance(data['data'], list) or 'next_cursor' not in data
                        or any(not isinstance(order, dict) or not isinstance(order.get('id'), str)
                               or not order['id'] for order in data['data'])):
                    raise ValueError('orders')
                count += len(data['data'])
                cursor = data.get('next_cursor')
                if cursor in (None, 'LTE='):
                    return dict(legacy_order_count=count, as_of=now)
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    break
                seen.add(cursor)
        except Exception:
            pass
        return dict(legacy_order_count=UNKNOWN, as_of=None)

    def read_region(self):
        try:
            self.__audit.require_ready()
            r = self.__http.get('https://polymarket.com/api/geoblock')
            r.raise_for_status()
            blocked = r.json().get('blocked')
            return 'ok' if blocked is False else 'blocked' if blocked is True else UNKNOWN
        except Exception:
            return UNKNOWN

    def wallet_tail(self):
        c = self.__credentials
        return c.wallet_address[-4:] if c is not None else UNKNOWN

    def read_fixed_market(self, now, condition_id):
        try:
            return self.__fixed_market(now, condition_id)
        except Exception:
            raise ValueError("fixed market validation unavailable") from None

    def __fixed_market(self, now, condition_id):
        self.__audit.require_ready()
        found=[]
        for page in self.__public.list_markets(condition_ids=[condition_id],include_tag=True,page_size=20):
            for raw in page.items:
                if str(raw.condition_id) != condition_id:
                    raise ValueError('fixed market identity mismatch')
                m=self.__metadata_market(raw,now,recovery=True)
                from lp_v2.scoring import score_market
                row=dict(title=raw.question or UNKNOWN,condition_id=condition_id,score=str(score_market(m).score))
                row.update(self.__map_market(raw,now,m,recovery=True))
                found.append(row)
        if len(found) != 1:
            raise ValueError('fixed market unavailable or ambiguous')
        return found

    def read_region_policy(self):
        self.__audit.require_ready()
        response=self.__http.get('https://polymarket.com/api/geoblock')
        response.raise_for_status()
        raw=response.json()
        if not isinstance(raw,dict):
            raise ValueError('region identity unavailable')
        return dict(blocked=raw.get('blocked'),country=raw.get('country'))


    def read_conditional_inventory(self, now, condition_id):
        """Fresh selected-market quantities, from verified tokens and authenticated GETs."""
        from decimal import Decimal
        from polymarket.models.clob.account import BalanceAllowance
        import json,re
        from polymarket._internal.wallet import classify_wallet_type
        from polymarket.environments import PRODUCTION
        try:
            c=self.__credentials
            if (c is None or type(c.signature_type) is not int or c.signature_type!=1
                    or classify_wallet_type(signer=c.signer_address,wallet=c.wallet_address,config=PRODUCTION.wallet_derivation)!='POLY_PROXY'
                    or not isinstance(condition_id,str) or not re.fullmatch('0x[0-9a-fA-F]{64}',condition_id)):
                raise ValueError()
            rows=self.__audit.fixed_market_wires(condition_id)
            if len(rows)!=1 or not isinstance(rows[0],dict) or rows[0].get('conditionId')!=condition_id:raise ValueError()
            wire=rows[0];tokens=wire.get('clobTokenIds');outcomes=wire.get('outcomes')
            if isinstance(tokens,str):tokens=json.loads(tokens)
            if isinstance(outcomes,str):outcomes=json.loads(outcomes)
            if (not isinstance(tokens,list) or len(tokens)!=2 or outcomes!=['Yes','No']
                    or not all(isinstance(t,str) and re.fullmatch('[0-9]+',t) for t in tokens)
                    or len(set(tokens))!=2):raise ValueError()
            tokens=tuple(tokens)
        except Exception:
            raise ConditionalIdentityUnavailable('CONDITIONAL_IDENTITY_UNAVAILABLE') from None
        quantities={}
        for token in tokens:
            raw=self.__account_get('/balance-allowance',now,dict(asset_type='CONDITIONAL',token_id=token,signature_type='1'))
            data=BalanceAllowance.model_validate(raw)
            if data.balance<0 or any(value<0 for value in data.allowances.values()):raise ValueError('conditional balance invalid')
            quantities[token]=Decimal(data.balance)/Decimal('1000000')
        return dict(condition_id=condition_id,token_ids=tokens,quantities=quantities,as_of=now.isoformat())

    def read_account_readiness(self, now, *, capital_usdc, identity, relayer):
        """Authenticated GET facts only. Authentication never proves a merge."""
        from decimal import Decimal
        from polymarket.environments import PRODUCTION
        from polymarket.models.clob.account import BalanceAllowance
        def fact(status,reason):
            return dict(status=status,reason=reason,as_of=now.isoformat())
        result={name:fact('UNKNOWN','ACCOUNT_EVIDENCE_MISSING') for name in
                ('balance','allowance','closed_only','legacy_orders','region','relayer_auth','relayer_merge','proxy_execution')}
        c=self.__credentials
        if (c is None or c.signer_address.lower()!=identity['signer_address'].lower()
                or c.wallet_address.lower()!=identity['wallet_address'].lower()
                or type(c.signature_type) is not int or c.signature_type!=1):
            return result
        try:
            from importlib.metadata import version
            from polymarket._internal.wallet import classify_wallet_type
            supported=(version('polymarket-client')=='0.2.0' and PRODUCTION.chain_id==137
                and PRODUCTION.relayer_url=='https://relayer-v2.polymarket.com'
                and classify_wallet_type(signer=c.signer_address,wallet=c.wallet_address,
                    config=PRODUCTION.wallet_derivation)=='POLY_PROXY')
            result['proxy_execution']=fact('PASS' if supported else 'FAIL','PINNED_PROXY_EXECUTION_CONFIGURATION')
        except Exception:
            pass
        try:
            raw=self.__account_get('/balance-allowance',now,dict(asset_type='COLLATERAL',signature_type='1'))
            data=BalanceAllowance.model_validate(raw)
            required=Decimal(capital_usdc)*1000000
            if data.balance<0 or any(value<0 for value in data.allowances.values()):
                raise ValueError()
            result['balance']=fact('PASS' if data.balance>=required else 'FAIL','BALANCE_CAPITAL')
            allowances={key.lower():value for key,value in data.allowances.items()}
            spenders=(PRODUCTION.standard_exchange,PRODUCTION.neg_risk_exchange)
            if all(key.lower() in allowances for key in spenders):
                result['allowance']=fact('PASS' if all(allowances[k.lower()]>=required for k in spenders) else 'FAIL','EXPLICIT_ALLOWANCE')
        except Exception:
            pass
        closed=None
        try:
            raw=self.__account_get('/auth/ban-status/closed-only',now,{})
            closed=raw.get('closed_only')
            if type(closed) is bool:
                result['closed_only']=fact('PASS' if closed is False else 'FAIL','CLOSED_ONLY')
        except Exception:
            pass
        orders=self.read_orders(now)
        count=orders['legacy_order_count']
        if type(count) is int:
            result['legacy_orders']=fact('PASS' if count==0 else 'FAIL','EXTERNAL_ORDERS_REQUIRE_HANDOFF')
        try:
            region=self.read_region_policy()
            allowed=region['blocked'] is False or (region['blocked'] is True and region['country']=='JP' and closed is False)
            if type(region['blocked']) is bool:
                result['region']=fact('PASS' if allowed else 'FAIL','JP_CLOSED_ONLY_EXCEPTION' if region['blocked'] is True and allowed else 'REGION_POLICY')
        except Exception:
            pass
        from lp_v2.relayer_credentials import parse_relayer_credentials
        from polymarket._internal.actions.relayer.auth import make_relayer_header_resolver_sync
        try:
            relayer_key=parse_relayer_credentials(relayer,identity['signer_address'])
        except ValueError:
            result['relayer_auth']=fact('FAIL','RELAYER_IDENTITY')
        else:
            try:
                self.__audit.require_ready()
                headers=make_relayer_header_resolver_sync(relayer_key)('GET','/transactions',None)
                response=self.__http.get(PRODUCTION.relayer_url+'/transactions',headers=headers)
                if response.status_code in (401,403):
                    result['relayer_auth']=fact('FAIL','RELAYER_AUTH_REJECTED')
                else:
                    response.raise_for_status()
                    rows=response.json()
                    if not isinstance(rows,list) or any(not isinstance(row,dict) or
                            not isinstance(row.get('owner'),str) or row['owner'].lower()!=identity['signer_address'].lower()
                            for row in rows):
                        raise ValueError()
                    result['relayer_auth']=fact('PASS','AUTHENTICATED_GET_ONLY')
            except Exception:
                pass
        result['relayer_merge']=fact('UNKNOWN','AUTH_DOES_NOT_PROVE_MERGE_READINESS')
        return result

    def public_preflight(self, now, condition_id):
        from lp_v2.preflight import check_market
        return check_market(self.__audit, self.__public, self.__http, now, condition_id, self.__metadata_market)
