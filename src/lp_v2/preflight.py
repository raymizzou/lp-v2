"""Observational public checks; statuses never imply private readiness."""
from decimal import Decimal as D
import json
import re
from lp_v2.fee_policy import wire_maker_policy


def check_market(audit, public, http, now, condition_id, metadata_validator, observation_time):
    names = ('market_identity','token_identity','fees','state','political','in_play',
             'rewards','constraints','books','opening_eligibility','market_inputs')
    checks = {name: dict(status='UNKNOWN',reason_code='DEPENDENCY_UNKNOWN') for name in names}
    for name in ('private_identity','relayer','allowance'):
        checks[name] = dict(status='NOT_CHECKED',reason_code='PUBLIC_SCOPE_ONLY')
    def set_check(name, passed, reason):
        checks[name] = dict(status='PASS' if passed else 'BLOCKED', reason_code='OK' if passed else reason)
    try:
        rows = audit.fixed_market_wires(condition_id)
    except Exception:
        checks['market_identity']['reason_code'] = 'PUBLIC_READ_UNAVAILABLE'
        return checks
    if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get('conditionId') != condition_id:
        set_check('market_identity',False,'MARKET_IDENTITY_MISMATCH')
        return checks
    wire = rows[0]
    set_check('market_identity',True,'OK')
    try:
        tokens = wire['clobTokenIds']
        outcomes = wire['outcomes']
        if isinstance(tokens,str): tokens=json.loads(tokens)
        if isinstance(outcomes,str): outcomes=json.loads(outcomes)
        valid = (isinstance(tokens,list) and len(tokens)==2 and len(set(tokens))==2
                 and all(isinstance(t,str) and re.fullmatch('[0-9]+',t) for t in tokens)
                 and outcomes==['Yes','No'])
    except Exception:
        valid=False
    set_check('token_identity',valid,'TOKEN_IDENTITY_INVALID')
    try:
        wire_maker_policy(wire)
        set_check('fees',True,'OK')
    except Exception:
        set_check('fees',False,'FEE_POLICY_UNPROVEN')
    set_check('state',all(wire.get(k) is v for k,v in (
        ('active',True),('closed',False),('acceptingOrders',True),('enableOrderBook',True))), 'MARKET_STATE_INVALID')
    tags = wire.get('tags')
    set_check('political',isinstance(tags,list) and any(isinstance(t,dict) and t.get('slug')=='politics'
                             for t in tags), 'POLITICAL_ELIGIBILITY_UNPROVEN')
    try:
        response=http.get('https://clob.polymarket.com/markets/'+condition_id)
        response.raise_for_status()
        state=response.json()
        if not isinstance(state,dict):raise ValueError('market state object required')
        set_check('in_play','in_play' not in state or state['in_play'] is False,'IN_PLAY_UNPROVEN')
    except Exception:
        checks['in_play']['reason_code']='PUBLIC_READ_UNAVAILABLE'
    def number(value, positive=False):
        if isinstance(value,bool): raise ValueError('number')
        n=D(str(value))
        if not n.is_finite() or n < 0 or (positive and n==0): raise ValueError('number')
        return n
    try:
        tick=number(wire.get('orderPriceMinTickSize'),True)
        minimum=number(wire.get('orderMinSize'),True)
        set_check('constraints',tick<1,'MARKET_CONSTRAINTS_INVALID')
    except Exception:
        set_check('constraints',False,'MARKET_CONSTRAINTS_INVALID')
    try:
        rates=[]
        for reward in wire.get('clobRewards') or []:
            if (reward.get('conditionId')==condition_id
                    and reward.get('assetAddress','').lower()=='0x2791bca1f2de4661ed88a30c99a7a9449aa84174'
                    and reward['startDate']<=now.date().isoformat()
                    and (reward.get('endDate') is None or reward['endDate']>=now.date().isoformat())):
                rates.append(number(reward['rewardsDailyRate']))
        number(wire.get('rewardsMinSize'),True)
        number(wire.get('rewardsMaxSpread'),True)
        set_check('rewards',bool(rates) and sum(rates)>0,'REWARDS_UNPROVEN')
    except Exception:
        set_check('rewards',False,'REWARDS_UNPROVEN')
    try:
        bid,ask=number(wire.get('bestBid'),True),number(wire.get('bestAsk'),True)
        set_check('opening_eligibility',0<bid<=ask<1 and D('.1')<=(bid+ask)/2<=D('.9')
                  and number(wire.get('liquidityNum'))>=1000,'OPENING_INELIGIBLE')
    except Exception:
        set_check('opening_eligibility',False,'OPENING_INELIGIBLE')
    if valid:
        # Share the scanner/startup metadata validator. Sanitize only fee fields here:
        # the real wire fee policy has its own independent required check above.
        try:
            from polymarket.models.gamma.market import Market
            sanitized = dict(wire, feesEnabled=False)
            sanitized.pop('feeSchedule',None)
            raw = Market.model_validate(sanitized)
            metadata_validator(raw,now,fee_wire=sanitized)
            set_check('market_inputs',True,'OK')
        except Exception:
            set_check('market_inputs',False,'MARKET_INPUTS_UNPROVEN')
    if valid and checks['constraints']['status']=='PASS':
        books_ok=True
        available=True
        first_prices=None
        timestamps=[]
        observed=now
        for token in tokens:
            try:
                b=public.get_order_book(token_id=token)
                observed=observation_time(observed)
                timestamps.append(b.timestamp)
                ok=(str(b.condition_id)==condition_id and str(b.token_id)==token
                    and b.tick_size==tick and b.min_order_size==minimum and b.bids and b.asks
                    and b.timestamp is not None and 0<=(observed-b.timestamp).total_seconds()<=60)
                if ok:
                    bid=max(level.price for level in b.bids);ask=min(level.price for level in b.asks)
                    ok=bid.is_finite() and ask.is_finite() and 0<bid<ask<1
                    if token==tokens[0] and ok:
                        first_prices=(bid,ask)
                books_ok=books_ok and bool(ok)
            except Exception:
                available=False
        try:
            completed=observation_time(observed)
            books_ok=books_ok and all(t is not None and 0<=(completed-t).total_seconds()<=60 for t in timestamps)
            now=completed
        except Exception:
            books_ok=False
        if not books_ok: set_check('books',False,'BOOK_IDENTITY_CONSTRAINTS_OR_FRESHNESS')
        elif available: set_check('books',True,'OK')
        else: checks['books']['reason_code']='PUBLIC_READ_UNAVAILABLE'
        # Opening eligibility follows the executable book midpoint, as in the scanner.
        # Retain independently proven metadata blockers if books are unavailable.
        if checks['books']['status']=='PASS':
            bid,ask=first_prices
            try:
                eligible=D('.1')<=(bid+ask)/2<=D('.9') and number(wire.get('liquidityNum'))>=1000
                set_check('opening_eligibility',eligible,'OPENING_INELIGIBLE')
            except Exception:
                set_check('opening_eligibility',False,'OPENING_INELIGIBLE')
        elif checks['opening_eligibility']['status']=='PASS':
            checks['opening_eligibility']=dict(status='UNKNOWN',reason_code='DEPENDENCY_UNKNOWN')
    details=dict(minimum_liquidity='1000',minimum_midpoint='0.10',maximum_midpoint='0.90',
                 as_of=now.isoformat(),blockers=[])
    try:
        liquidity=number(wire.get('liquidityNum'))
        details['liquidity_num']=format(liquidity,'f')
        if liquidity<1000:details['blockers'].append('LIQUIDITY_BELOW_MIN')
    except Exception:pass
    if checks['books']['status']=='PASS':
        midpoint=sum(first_prices)/2
        details['midpoint']=format(midpoint,'f')
        if not D('.10')<=midpoint<=D('.90'):details['blockers'].append('MIDPOINT_OUTSIDE_RANGE')
    checks['opening_eligibility']['details']=details
    return checks


def run_preflight(config):
    from lp_v2.readonly_sdk import ReadOnlySDK
    sdk=None
    try:
        sdk=ReadOnlySDK()
        checks=sdk.public_preflight(config.started_at,config.market)
    except Exception:
        checks={'public_market':dict(status='UNKNOWN',reason_code='PUBLIC_READ_UNAVAILABLE')}
        for name in ('private_identity','relayer','allowance'):
            checks[name]=dict(status='NOT_CHECKED',reason_code='PUBLIC_SCOPE_ONLY')
    finally:
        if sdk is not None:
            try: sdk.close()
            except Exception:
                checks['public_transport']=dict(status='UNKNOWN',reason_code='PUBLIC_READ_UNAVAILABLE')
    required=[c['status'] for c in checks.values() if c['status']!='NOT_CHECKED']
    status='BLOCKED' if 'BLOCKED' in required else 'UNKNOWN' if 'UNKNOWN' in required else 'PASS'
    return dict(scope='public_market',code_sha=config.code_sha,market=config.market,
                capital_usdc=str(config.capital_usdc),as_of=checks.get('opening_eligibility',{}).get('details',{}).get('as_of',config.started_at.isoformat()),
                status=status,checks=checks)
