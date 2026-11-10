"""Safe observational inventory diagnostics; never an accounting adjustment."""
from decimal import Decimal

UNKNOWN = 'UNKNOWN'


def inventory_numbers(tokens, quantities=None, recorded=None, evidence=None):
    quantities, recorded, evidence = quantities or {}, recorded or {}, evidence or {}
    rows = []
    for i, token in enumerate(tokens):
        exchange, ledger = quantities.get(token, UNKNOWN), recorded.get(token, UNKNOWN)
        difference = exchange-ledger if isinstance(exchange, Decimal) and isinstance(ledger, Decimal) else UNKNOWN
        rows.append(dict(token=str(i+1), exchange=exchange, ledger=ledger, difference=difference,
            explanation=('exchange_balance_differs_from_historical_ledger' if difference not in (UNKNOWN, 0)
                         else 'historical_ledger_matches' if difference == 0 else 'inventory_evidence_incomplete'),
            **{key:evidence.get(key, UNKNOWN) for key in ('source','observed_at','exchange_as_of',
                'upstream_unit','upstream_precision','adapter_divisor')}))
    return dict(inventory=rows)


def safe_details(row):
    """Allow only fixed labels and bounded numeric diagnostics through remote CLI."""
    def number(value):
        try:
            if isinstance(value,bool) or len(str(value))>100:
                return UNKNOWN
            parsed=Decimal(str(value))
            return str(parsed) if parsed.is_finite() else UNKNOWN
        except Exception:
            return UNKNOWN
    result={}
    raw=row.get('numbers',{})
    rows=raw.get('inventory',[]) if isinstance(raw,dict) else []
    if isinstance(rows,(list,tuple)) and len(rows)==2:
        clean=[]
        for i,item in enumerate(rows):
            item=item if isinstance(item,dict) else {}
            clean.append(dict(token=str(i+1),**{k:number(item.get(k)) for k in ('exchange','ledger','difference')},
                **{k:(item.get(k) if item.get(k) in allowed else UNKNOWN) for k,allowed in {
                    'source':('authenticated GET /balance-allowance CONDITIONAL',),
                    'upstream_unit':(), 'upstream_precision':(), 'exchange_as_of':(),
                    'adapter_divisor':('1000000',),
                    'explanation':('exchange_balance_differs_from_historical_ledger','historical_ledger_matches','inventory_evidence_incomplete'),
                }.items()}))
            from datetime import datetime
            try:
                at=datetime.fromisoformat(item['observed_at'])
                clean[-1]['observed_at']=at.isoformat() if at.tzinfo else UNKNOWN
            except Exception:
                clean[-1]['observed_at']=UNKNOWN
        result['numbers']=dict(inventory=clean)
    for key in ('owned_open_count','merge_count','non_target_order_count'):
        value=row.get(key)
        result[key]=value if type(value) is int and 0<=value<=100000 else UNKNOWN
    tails=row.get('foreign_order_tails')
    if isinstance(tails,list):
        import re
        result['foreign_order_tails']=[t for t in tails[:100] if isinstance(t,str) and re.fullmatch('[A-Za-z0-9_-]{1,8}',t)]
    return result


def order_identity_digest(ids):
    import hashlib,json
    return hashlib.sha256(json.dumps(sorted(set(ids)),separators=(',',':')).encode()).hexdigest()


def absence_step(expiry, candidates, present_ids, at, previous=(None,0)):
    """Shared execution-only GTD absence rule; no accounting terminal inference."""
    from datetime import timedelta
    if not candidates or set(candidates)&set(present_ids) or at<expiry+timedelta(seconds=120):
        return None,0
    last,count=previous
    return (at,min(2,count+1)) if last is None or at>last else previous
