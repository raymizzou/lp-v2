"""Read-only validation of a complete stopped snapshot, never ledger replay/adoption."""
from pathlib import Path
from decimal import Decimal


def inventory_handoff(profile,balances, *, orders=None, absence_orders=()):
    from lp_v2.live_control.host import fact,journal_rows,journal_present,merge_history_report
    from lp_v2.live_control.profiles import read_json,timestamp
    from lp_v2.live_control.workflow import fresh
    from lp_v2.models import Position,Order,OrderStatus
    from lp_v2.live_control.identity import same_identity,single_market_tokens
    from lp_v2.inventory_evidence import inventory_numbers, safe_details
    details=dict(numbers=inventory_numbers(('1','2')),owned_open_count='UNKNOWN',merge_count='UNKNOWN')
    def answer(status,reason):
        return dict(fact(status,reason),**safe_details(details))
    try:
        from lp_v2.rotation_state import control_market_profile,run_journal_sequence
        active_profile=control_market_profile(profile)
        market=active_profile['trading']['market_condition_id'];tokens=balances['token_ids'];quantities=balances['quantities']
        if (not fresh(balances['as_of']) or balances['condition_id']!=market or len(tokens)!=2 or len(set(tokens))!=2
                or set(quantities)!=set(tokens) or any(not money(q) for q in quantities.values())):raise ValueError()
        details['numbers']=inventory_numbers(tokens,quantities,evidence=balances)
        if isinstance(orders,dict):details['owned_open_count']=orders.get('owned_open_count','UNKNOWN')
        state=Path(profile['files']['state_dir'])
        if not journal_present(profile):
            if any((state/name).exists() for name in ('live-control-run.json','process.json','stop-result.json')):raise ValueError()
            zero=all(q==0 for q in quantities.values())
            return answer('PASS' if zero else 'FAIL','NEW_STATE_ZERO_CONDITIONAL_BALANCES' if zero else 'CONDITIONAL_INVENTORY_UNMATCHED')
        rows=journal_rows(profile)
        owner='lp-v2:'+profile['identity']['wallet_address'].lower()
        identities=[value for _,kind,value in rows if kind=='identity']
        if not identities or any(single_market_tokens(identity,owner,market)!=tuple(tokens) for identity in identities):raise ValueError()
        control=read_json(state/'live-control-run.json');process=read_json(state/'process.json');final=read_json(state/'stop-result.json')
        run_id=control['run_id']
        import re
        from lp_v2.live_process import stop_result_matches
        if (not isinstance(run_id,str) or not re.fullmatch('[0-9a-f]{32}',run_id)
                or process['run_id']!=run_id or not stop_result_matches(process,final)
                or type(process['pid']) is not int or process['pid']<=1 or not isinstance(process['identity'],str) or not process['identity']
                or not same_identity(control['profile']['identity'],profile['identity'])
                or control_market_profile(control['profile'])['trading']['market_condition_id']!=market
                or Path(control['profile']['files']['state_dir']).resolve()!=state.resolve()
                or type(final['exit_code']) is not int or final['exit_code'] not in (0,1)):raise ValueError()
        # stop_result is the authoritative final financial snapshot. Any later event
        # means that this stopped run no longer explains the current journal.
        seq,kind,drain=rows[-1]
        occupations=[(s,v) for s,k,v in rows if k=='occupation']
        if not occupations or kind!='stop_result':raise ValueError()
        occupation_seq,occupation=occupations[-1]
        started=timestamp(control['started_at']);at=timestamp(drain['at'].isoformat())
        accounting=timestamp(drain['accounting_as_of'].isoformat()) if drain.get('accounting_as_of') is not None else None
        if (not run_journal_sequence(control,market)<occupation_seq<seq
                or timestamp(occupation['at'].isoformat())<started or not started<at
                or type(drain['exit_code']) is not int or drain['exit_code']!=final['exit_code']
                or not isinstance(drain['pending'],(list,tuple))
                or not isinstance(drain['orders'],(list,tuple)) or not isinstance(drain['positions'],(list,tuple))
                ):raise ValueError()
        for order in drain['orders']:
            if (not isinstance(order,Order) or not isinstance(order.order_id,str) or not order.order_id
                    or order.request.owner!=owner or order.request.quote.condition_id!=market or order.request.quote.token_id not in tokens
                    or not money(order.remaining_quantity) or not money(order.filled_quantity)
                    or order.observed_at.tzinfo is None or order.observed_at>drain['at']):raise ValueError()
        owned={token:Decimal('0') for token in tokens};seen=set()
        for position in drain['positions']:
            if (not isinstance(position,Position) or position.owner!=owner or position.condition_id!=market or position.token_id not in tokens
                    or position.token_id in seen or not money(position.quantity) or not money(position.cost_usdc)
                    or position.updated_at.tzinfo is None or position.updated_at>drain['at']
                    or (position.quantity==0 and position.cost_usdc!=0)):raise ValueError()
            seen.add(position.token_id);owned[position.token_id]=position.quantity
        details['numbers']=inventory_numbers(tokens,quantities,owned,balances)
        details['merge_count']=len(drain['merges']) if isinstance(drain.get('merges'),(tuple,list)) else 'UNKNOWN'
        expected={v['request'].merge_id for _,k,v in rows if k=='merge_intent'}
        if (merge_history_report(active_profile,drain.get('merges'))['status']!='PASS'
                or expected!={m.request.merge_id for m in drain['merges']}):raise ValueError()
        for merge in drain['merges']:
            if (merge.request.yes_token_id,merge.request.no_token_id)!=tuple(tokens) or merge.observed_at>drain['at']:raise ValueError()
        # Current execution evidence gates every route, including old successful
        # stopped snapshots. P7's foreign count deliberately excludes these ids.
        if (not isinstance(orders,dict) or orders.get('complete') is not True
                or orders.get('condition_id')!=market
                or not fresh(orders['as_of'].isoformat() if hasattr(orders.get('as_of'),'isoformat') else orders.get('as_of'))
                or type(orders.get('owned_open_count')) is not int or orders['owned_open_count']<0):
            return answer('UNKNOWN','STOPPED_HANDOFF_INCOMPLETE')
        if orders['owned_open_count']>0:
            return answer('FAIL','OWNED_OPEN_ORDERS_REMAIN')
        zero=all(q==0 for q in quantities.values())
        if not zero and (final['exit_code']!=0 or drain['reconciliation']!='SUCCESS' or owned!=quantities):
            return answer('FAIL','CONDITIONAL_INVENTORY_UNMATCHED')
        if drain['pending'] and (not zero or not pending_absence_verified(profile,control,rows,drain,orders,absence_orders)):
            raise ValueError()
        if (zero and isinstance(orders,dict) and orders.get('complete') is True and orders.get('condition_id')==market
                and fresh(orders['as_of'].isoformat() if hasattr(orders.get('as_of'),'isoformat') else orders.get('as_of')) and orders.get('owned_open_count')==0):
            return answer('PASS','FRESH_ZERO_EXECUTION_RISK_CLEARED')
        if (drain['pending'] or final['exit_code']!=0 or drain['reconciliation']!='SUCCESS'
                or any(o.authoritative is not True or o.status not in (OrderStatus.FILLED,OrderStatus.CANCELLED)
                       or o.remaining_quantity!=0 for o in drain['orders'])
                or accounting is None or not started<accounting<=at
                or any(p.updated_at>accounting for p in drain['positions'])):
            raise ValueError()
        if owned!=quantities:return answer('FAIL','CONDITIONAL_INVENTORY_UNMATCHED')
        return answer('PASS','OWNED_STOPPED_INVENTORY_MATCH')
    except Exception:
        return answer('UNKNOWN','STOPPED_HANDOFF_INCOMPLETE')


def money(value):
    return isinstance(value,Decimal) and value.is_finite() and value>=0


def pending_absence_verified(profile, control, rows, drain, first, remaining):
    """Keep pending money UNKNOWN; verify only expired signed execution is absent."""
    from datetime import timedelta
    from lp_v2.models import OrderRequest
    from lp_v2.inventory_evidence import order_identity_digest,absence_step
    from lp_v2.live_control.host import owned_order_ids
    from lp_v2.live_control.profiles import timestamp
    from lp_v2.live_control.workflow import fresh
    import re
    try:
        market=drain['pending'][0].quote.condition_id
        identities=[v for _,k,v in rows if k=='identity']
        from lp_v2.live_control.identity import single_market_tokens
        owner='lp-v2:'+profile['identity']['wallet_address'].lower()
        tokens=single_market_tokens(identities[0],owner,market)
        signed_ids=set();requests=[];seen=set()
        for request in drain['pending']:
            if (not isinstance(request,OrderRequest) or request.owner!=owner or request.quote.condition_id!=market
                    or request.quote.token_id not in tokens or request.request_id in seen
                    or request.reservation_terms.token_ids!=tokens
                    or not money(request.quote.quantity) or request.quote.quantity<=0
                    or not money(request.quote.price) or not 0<request.quote.price<1
                    or request.expires_at!=request.created_at+timedelta(hours=1)):
                return False
            seen.add(request.request_id)
            intents=[v['request'] for _,k,v in rows if k=='intent' and v['request'].request_id==request.request_id]
            signatures=[v for _,k,v in rows if k=='signed' and v.get('request_id')==request.request_id]
            if intents!=[request] or len(signatures)!=1:return False
            signed=signatures[0];ids=signed['order_ids']
            if (not isinstance(ids,(tuple,list)) or len(ids)!=2 or len(set(ids))!=2
                    or any(not isinstance(oid,str) or not re.fullmatch('0x[0-9a-fA-F]{64}',oid) for oid in ids)
                    or signed['token_id']!=request.quote.token_id
                    or signed['maker'].lower()!=profile['identity']['wallet_address'].lower()
                    or signed['signer'].lower()!=profile['identity']['signer_address'].lower()
                    or signed['expiration']!=int(request.expires_at.timestamp())
                    or signed['action_id']!=control['action_id'] or signed['window_end']!=timestamp(control['window_end'])
                    or not timestamp(control['started_at'])<=request.created_at<=signed['signed_at']<signed['window_end']
                    or signed['signed_at']>drain['at']
                    or type(signed['maker_amount']) is not int or signed['maker_amount']!=int(request.quote.price*request.quote.quantity*1000000)
                    or type(signed['taker_amount']) is not int or signed['taker_amount']!=int(request.quote.quantity*1000000)):
                return False
            signed_ids.update(ids);requests.append(request)
        digest=order_identity_digest(owned_order_ids(profile))
        observations=(first,*remaining)
        if len(observations)!=2:return False
        for request in requests:
            prior=(None,0)
            for observation in observations:
                at=observation.get('as_of')
                at=timestamp(at.isoformat() if hasattr(at,'isoformat') else at)
                if (observation.get('complete') is not True or observation.get('condition_id')!=market
                        or observation.get('owned_open_count')!=0 or observation.get('owned_ids_sha256')!=digest
                        or not fresh(at.isoformat())):
                    return False
                prior=absence_step(request.expires_at,signed_ids,(),at,prior)
            if prior[1]!=2:return False
        return bool(requests)
    except Exception:
        return False
