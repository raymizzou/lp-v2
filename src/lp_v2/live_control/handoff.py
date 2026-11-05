"""Read-only validation of a complete stopped snapshot, never ledger replay/adoption."""
from pathlib import Path
from decimal import Decimal


def inventory_handoff(profile,balances):
    from lp_v2.live_control.host import fact,journal_rows,journal_present,merge_history_report
    from lp_v2.live_control.profiles import read_json,timestamp
    from lp_v2.live_control.workflow import fresh
    from lp_v2.models import Position,Order,OrderStatus
    from lp_v2.live_control.identity import same_identity
    try:
        market=profile['trading']['market_condition_id'];tokens=balances['token_ids'];quantities=balances['quantities']
        if (not fresh(balances['as_of']) or balances['condition_id']!=market or len(tokens)!=2 or len(set(tokens))!=2
                or set(quantities)!=set(tokens) or any(not money(q) for q in quantities.values())):raise ValueError()
        state=Path(profile['files']['state_dir'])
        if not journal_present(profile):
            if any((state/name).exists() for name in ('live-control-run.json','process.json','stop-result.json')):raise ValueError()
            zero=all(q==0 for q in quantities.values())
            return fact('PASS' if zero else 'UNKNOWN','NEW_STATE_ZERO_CONDITIONAL_BALANCES' if zero else 'CONDITIONAL_INVENTORY_UNMATCHED')
        rows=journal_rows(profile)
        owner='lp-v2:'+profile['identity']['wallet_address'].lower()
        identities=[value for _,kind,value in rows if kind=='identity']
        expected=dict(owner=owner,condition_id=market,token_ids=tuple(tokens))
        if not identities or any(identity!=expected for identity in identities):raise ValueError()
        control=read_json(state/'live-control-run.json');process=read_json(state/'process.json');final=read_json(state/'stop-result.json')
        run_id=control['run_id']
        import re
        if (not isinstance(run_id,str) or not re.fullmatch('[0-9a-f]{32}',run_id)
                or process['run_id']!=run_id or final['run_id']!=run_id
                or type(process['pid']) is not int or process['pid']<=1 or not isinstance(process['identity'],str) or not process['identity']
                or not same_identity(control['profile']['identity'],profile['identity'])
                or control['profile']['trading']['market_condition_id']!=market
                or Path(control['profile']['files']['state_dir']).resolve()!=state.resolve()
                or type(final['exit_code']) is not int or final['exit_code']!=0):raise ValueError()
        # stop_result is the authoritative final financial snapshot. Any later event
        # means that this stopped run no longer explains the current journal.
        seq,kind,drain=rows[-1]
        occupations=[(s,v) for s,k,v in rows if k=='occupation']
        if not occupations or kind!='stop_result':raise ValueError()
        occupation_seq,occupation=occupations[-1]
        started=timestamp(control['started_at']);at=timestamp(drain['at'].isoformat());accounting=timestamp(drain['accounting_as_of'].isoformat())
        if (type(control['journal_seq']) is not int or not control['journal_seq']<occupation_seq<seq
                or timestamp(occupation['at'].isoformat())<started or not started<accounting<=at
                or type(drain['exit_code']) is not int or drain['exit_code']!=0 or drain['reconciliation']!='SUCCESS'
                or not isinstance(drain['pending'],(list,tuple)) or drain['pending']
                or not isinstance(drain['orders'],(list,tuple)) or not isinstance(drain['positions'],(list,tuple))
                or merge_history_report(profile,drain.get('merges'))['status']!='PASS'):raise ValueError()
        for merge in drain['merges']:
            if (merge.request.yes_token_id,merge.request.no_token_id)!=tuple(tokens) or merge.observed_at>drain['at']:raise ValueError()
        for order in drain['orders']:
            if (not isinstance(order,Order) or order.authoritative is not True or order.status not in (OrderStatus.FILLED,OrderStatus.CANCELLED)
                    or order.request.owner!=owner or order.request.quote.condition_id!=market or order.request.quote.token_id not in tokens
                    or not money(order.remaining_quantity) or order.remaining_quantity!=0 or not money(order.filled_quantity)
                    or order.observed_at.tzinfo is None or order.observed_at>drain['at']):raise ValueError()
        owned={token:Decimal('0') for token in tokens};seen=set()
        for position in drain['positions']:
            if (not isinstance(position,Position) or position.owner!=owner or position.condition_id!=market or position.token_id not in tokens
                    or position.token_id in seen or not money(position.quantity) or not money(position.cost_usdc)
                    or position.updated_at.tzinfo is None or position.updated_at>drain['accounting_as_of']
                    or (position.quantity==0 and position.cost_usdc!=0)):raise ValueError()
            seen.add(position.token_id);owned[position.token_id]=position.quantity
        if owned!=quantities:return fact('UNKNOWN','CONDITIONAL_INVENTORY_UNMATCHED')
        return fact('PASS','OWNED_STOPPED_INVENTORY_MATCH')
    except Exception:
        return fact('UNKNOWN','STOPPED_HANDOFF_INCOMPLETE')


def money(value):
    return isinstance(value,Decimal) and value.is_finite() and value>=0
