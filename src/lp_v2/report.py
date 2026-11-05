"""UTC reports from one read-only SQLite snapshot and fresh GET-only L2 income."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
import csv
import json
import os
import tempfile

from lp_v2.journal import Journal
from lp_v2.models import Fill, Outcome, OrderStatus

UNKNOWN = 'UNKNOWN'
COLUMNS = ('reward','rebate','paired_profit','fees','inventory_delta','net_profit','average_occupation','yield_pct_per_day')


def output_paths(state_dir, out):
    state=Path(state_dir).resolve(strict=True)
    if not state.is_dir():
        raise ValueError('state directory required')
    prefix=Path(out)
    targets=(prefix.with_suffix('.csv'),prefix.with_suffix('.json'))
    protected={(p.stat().st_dev,p.stat().st_ino) for p in state.rglob('*') if p.is_file()}
    for path in (prefix,*targets):
        resolved=path.resolve()
        if resolved == state or state in resolved.parents or path.is_symlink():
            raise ValueError('output must be outside state')
        if path.exists() and (not path.is_file() or (path.stat().st_dev,path.stat().st_ino) in protected):
            raise ValueError('output aliases protected state')
    return targets


def _number(value):
    if isinstance(value,bool):
        raise ValueError('invalid amount')
    n=D(str(value))
    if not n.is_finite() or n < 0:
        raise ValueError('invalid amount')
    return n


def _daily_income(credentials, wallet, day, markets):
    result={cid:dict(reward=UNKNOWN,rebate=UNKNOWN,notes=[]) for cid in markets}
    if credentials is None or credentials.wallet_address.lower() != wallet.lower():
        for row in result.values():row['notes'].append('income: explicit matching read-only L2 credentials unavailable')
        return result
    import base64, hashlib, hmac, re
    import httpx
    from polymarket import PublicClient
    from polymarket.environments import PRODUCTION
    from lp_v2.readonly_transport import ReadOnlyTransportAudit
    public=PublicClient()
    http=httpx.Client(timeout=5,trust_env=False,follow_redirects=False)
    try:
        audit=ReadOnlyTransportAudit(public,http)
        audit.require_ready()
        c=credentials
        if (not all(isinstance(v,str) and v for v in (c.key,c.secret,c.passphrase))
                or not all(re.fullmatch('0x[0-9a-fA-F]{40}',v) for v in (c.signer_address,c.wallet_address))
                or type(c.signature_type) is not int or c.signature_type not in (0,1,2)):
            raise ValueError('invalid L2 identity')
        secret=base64.b64decode(c.secret+'='*(-len(c.secret)%4),altchars=b'-_',validate=True)
        if not secret:raise ValueError('empty L2 secret')
        assets={PRODUCTION.collateral_token.lower(),'0x2791bca1f2de4661ed88a30c99a7a9449aa84174'}
        def pages(path):
            cursor,seen=None,set()
            while True:
                params=dict(date=day,signature_type=c.signature_type) if path=='/rewards/user' else dict(date=day,maker_address=wallet)
                if cursor is not None:params['next_cursor']=cursor
                timestamp=str(int(datetime.now(UTC).timestamp()))
                signature=base64.urlsafe_b64encode(hmac.new(secret,(timestamp+'GET'+path).encode(),hashlib.sha256).digest()).decode()
                response=http.get('https://clob.polymarket.com'+path,params=params,headers=dict(
                    POLY_ADDRESS=c.signer_address,POLY_API_KEY=c.key,POLY_PASSPHRASE=c.passphrase,
                    POLY_TIMESTAMP=timestamp,POLY_SIGNATURE=signature))
                response.raise_for_status();data=response.json()
                if path=='/rebates/current' and isinstance(data,list):
                    yield data
                    return
                if not isinstance(data,dict) or not isinstance(data.get('data'),list) or 'next_cursor' not in data:
                    raise ValueError('incomplete income page')
                yield data['data']
                cursor=data['next_cursor']
                if cursor in (None,'LTE='):return
                if not isinstance(cursor,str) or not cursor or cursor in seen:
                    raise ValueError('invalid income cursor')
                seen.add(cursor)
        for column,path in (('reward','/rewards/user'),('rebate','/rebates/current')):
            try:
                totals=dict.fromkeys(markets,D('0'))
                for rows in pages(path):
                    for raw in rows:
                        cid=raw['condition_id']
                        if cid not in markets:continue
                        if column=='reward':
                            from polymarket.models.clob.rewards import UserEarning
                            row=UserEarning.model_validate(raw)
                            observed_day=row.date.astimezone(UTC).date().isoformat()
                            maker,asset=row.maker_address,row.asset_address
                            amount=_number(row.earnings)*_number(row.asset_rate)
                        else:
                            observed_day=raw['date'];maker,asset=raw['maker_address'],raw['asset_address']
                            amount=_number(raw['rebated_fees_usdc'])
                        if observed_day != day or maker.lower() != wallet.lower() or asset.lower() not in assets:
                            raise ValueError('income provenance mismatch')
                        # Repeated identical rows may be distinct assets/earnings; pagination itself must terminate.
                        totals[cid]+=amount
                for cid,value in totals.items():result[cid][column]=value
            except Exception:
                for row in result.values():row['notes'].append(column+': GET income unavailable, incomplete or identity mismatch')
    except Exception:
        for row in result.values():row['notes'].append('income: read-only authentication or transport unavailable')
    finally:
        http.close();public.close()
    return result


def _gross(fills, merges, cid, day):
    basis,fees,paired={},D('0'),D('0')
    events=[(f.executed_at,0,f.trade_id,f) for f in fills if f.condition_id==cid]
    events += [(r.observed_at,1,r.request.merge_id,r) for r in merges if r.request.condition_id==cid and r.received_usdc]
    for at,_,_,fact in sorted(events):
        if at.astimezone(UTC).date()>day:continue
        if isinstance(fact,Fill):
            if not 0<_number(fact.price)<1 or _number(fact.quantity)<=0:
                raise ValueError('invalid fill amount')
            _number(fact.fee_usdc)
            q,c=basis.get(fact.token_id,(D('0'),D('0')))
            basis[fact.token_id]=(q+fact.quantity,c+fact.quantity*fact.price)
            if at.astimezone(UTC).date()==day:fees+=fact.fee_usdc
        else:
            cost=D('0')
            for leg in fact.consumed_positions:
                q,c=basis[leg.token_id]
                if leg.quantity>q or leg.quantity<=0:raise ValueError('merge cost proof incomplete')
                consumed=c if q==leg.quantity else c*leg.quantity/q
                basis[leg.token_id]=(q-leg.quantity,c-consumed);cost+=consumed
            if at.astimezone(UTC).date()==day:paired+=fact.received_usdc-cost
    return paired,fees


def _market(sample,cid,legacy):
    return sample.get('markets',{}).get(cid) if 'markets' in sample else sample if cid==legacy else None


def _coverage(samples,start,end):
    by_at={p['at']:p for p in samples}
    samples=sorted(by_at.values(),key=lambda p:p['at'])
    before=[p for p in samples if p['at']<=start]
    after=[p for p in samples if p['at']>=end]
    if not before or not after:return ()
    chosen=[before[-1],*(p for p in samples if start<p['at']<end),after[0]]
    return tuple(chosen) if all(0<(b['at']-a['at']).total_seconds()<=60 for a,b in zip(chosen,chosen[1:])) else ()


def _average(samples,cid,legacy,start,end):
    chosen=_coverage(samples,start,end)
    if not chosen:return UNKNOWN
    integral=D('0')
    for a,b in zip(chosen,chosen[1:]):
        if a.get('reconciliation')!='SUCCESS':return UNKNOWN
        market=_market(a,cid,legacy)
        if market is None:return UNKNOWN
        seconds=D(str((min(b['at'],end)-max(a['at'],start)).total_seconds()))
        integral+=_number(market['total'])*seconds
    return integral/D('86400')


def _safety(samples,events,start,end,legacy,now):
    coverage=_coverage(samples,start,end)
    checks={k:[] if coverage else [UNKNOWN] for k in ('capital','unpaired','hedge_ceiling','duplicate_hedge')}
    for p in samples:
        if not start<=p['at']<end:continue
        try:
            cap=_number(p['startup_cap']);total=_number(p['total'])
            checks['capital'].append('FAIL' if cap<=0 or total>min(cap,D('100')) else 'PASS' if p.get('reconciliation')=='SUCCESS' else UNKNOWN)
        except Exception:checks['capital'].append(UNKNOWN)
        markets=p.get('markets',{legacy:p})
        cost=D('0');risk=[]
        for cid,m in markets.items():
            try:
                planned=_number(m['planned_quantity']);inventory=m['inventory'];hedges=m['hedges']
                if p.get('reconciliation')!='SUCCESS':raise ValueError('stale risk proof')
                risk.append('FAIL' if any(_number(i['unpaired_quantity'])>planned for i in inventory) else 'PASS')
                cost+=sum((_number(i['unpaired_cost_usdc']) for i in inventory),D('0'))
                for h in hedges:
                    checks['hedge_ceiling'].append('PASS' if _number(h['price'])<=_number(h['ceiling']) else 'FAIL')
                quantities={i['token_id']:_number(i['unpaired_quantity']) for i in inventory}
                for token in {h['token_id'] for h in hedges}:
                    quantity=sum((_number(h['quantity']) for h in hedges if h['token_id']==token),D('0'))
                    opposite=sum((v for t,v in quantities.items() if t!=token),D('0'))
                    checks['duplicate_hedge'].append('FAIL' if quantity>opposite else 'PASS')
                if not hedges:
                    checks['hedge_ceiling'].append('PASS');checks['duplicate_hedge'].append('PASS')
            except Exception:
                risk.append(UNKNOWN);checks['hedge_ceiling'].append(UNKNOWN);checks['duplicate_hedge'].append(UNKNOWN)
        try:risk.append('FAIL' if cost>_number(p['startup_cap'])/2 else 'PASS')
        except Exception:risk.append(UNKNOWN)
        checks['unpaired'].extend(risk)
    opened,episodes={},[]
    def settle(key,at,outcome):
        if key not in opened:return
        began=opened.pop(key)
        if began<end and at>=start:
            episodes.append(dict(identity=key[1],kind=key[0],started_at=began.isoformat(),resolved_at=at.isoformat(),seconds=str(max(0.,(at-began).total_seconds())),outcome=outcome,reason='durable authoritative result'))
    for kind,p in events:
        if kind=='intent':
            r=p['request'];opened.setdefault(('placement',r.request_id),r.created_at)
            if r.purpose=='hedge' and start<=r.created_at<end:
                try:checks['hedge_ceiling'].append('PASS' if _number(r.quote.price)<=_number(p['hedge_ceiling']) else 'FAIL')
                except Exception:checks['hedge_ceiling'].append(UNKNOWN)
        elif kind=='order_result':
            if p.outcome==Outcome.FAILED and p.order is None or p.outcome==Outcome.SUCCESS and p.order is not None and p.order.authoritative:
                settle(('placement',p.request.request_id),p.observed_at,p.outcome.value)
                if p.order is not None and p.order.status in (OrderStatus.CANCELLED,OrderStatus.FILLED):
                    settle(('cancel',p.order.order_id),p.observed_at,p.outcome.value)
            else:opened.setdefault(('placement',p.request.request_id),p.observed_at)
        elif kind=='cancel_intent':opened.setdefault(('cancel',p['order_id']),p['at'])
        elif kind=='merge_intent':opened.setdefault(('merge',p['request'].merge_id),p['request'].created_at)
        elif kind=='merge_result':
            if p.outcome==Outcome.FAILED or p.outcome==Outcome.SUCCESS and p.received_usdc>0:
                settle(('merge',p.request.merge_id),p.observed_at,p.outcome.value)
    for (kind,key),began in opened.items():
        if began<end:
            episodes.append(dict(identity=key,kind=kind,started_at=began.isoformat(),resolved_at=None,seconds=str(max(0.,(now-began).total_seconds())),outcome=UNKNOWN,reason='no durable authoritative resolution'))
    result={}
    for name,values in checks.items():
        status='FAIL' if 'FAIL' in values else UNKNOWN if not values or UNKNOWN in values else 'PASS'
        result[name]=dict(status=status,reason='observed violation' if status=='FAIL' else 'missing or stale risk evidence' if status==UNKNOWN else 'all recorded proofs within bounds')
    result['unknown_duration']=dict(status=UNKNOWN,reason='Ray has not set a live UNKNOWN duration threshold',episodes=episodes)
    return result


def write_report(config):
    targets=output_paths(config.state_dir,config.out)
    day=date.fromisoformat(config.day);start=datetime.combine(day,datetime.min.time(),UTC);end=start+timedelta(days=1)
    journal=Journal(Path(config.state_dir)/'journal.sqlite',readonly=True)
    try:
        # One SQLite read transaction sees committed WAL facts without immutable=1 or source writes.
        events=journal.events()
        snapshot_seq=journal.db.execute('SELECT max(seq) FROM events').fetchone()[0]
    finally:journal.close()
    identities=[p for kind,p in events if kind=='identity']
    if not identities:raise ValueError('missing durable wallet identity')
    owners={p['owner'] for p in identities}
    if len(owners)!=1:raise ValueError('wallet identity changed')
    owner=owners.pop();wallet=owner.removeprefix('lp-v2:')
    pairs={}
    for p in identities:
        for cid,pair in p.get('markets',{p.get('condition_id'):p.get('token_ids')}).items():
            if cid in pairs and pairs[cid]!=tuple(pair):raise ValueError('token identity changed')
            pairs[cid]=tuple(pair)
    legacy=identities[0].get('condition_id',next(iter(identities[0].get('markets',pairs))))
    samples=[p for k,p in events if k=='occupation']
    selected=set()
    for p in samples:
        if start<=p['at']<end:selected.update(p.get('markets',{p.get('condition_id'):None}))
    baselines=[p for k,p in events if k=='day_baseline']
    for p in baselines:
        if p['day']==config.day:selected.add(p.get('condition_id',legacy))
    if not selected:
        for p in identities:
            if p.get('at',start)<end:selected.update(p.get('markets',{p.get('condition_id'):None}))
    selected.discard(None)
    if not selected or any(cid not in pairs for cid in selected):raise ValueError('historical market identity unavailable')
    selected=tuple(cid for cid in pairs if cid in selected)
    fills,merges={},{}
    for kind,p in events:
        if kind=='fill':
            if p.owner!=owner or p.condition_id not in pairs or p.token_id not in pairs[p.condition_id]:raise ValueError('fill identity mismatch')
            if p.trade_id in fills and fills[p.trade_id]!=p:raise ValueError('fill identity changed')
            fills[p.trade_id]=p
        if kind=='merge_result':
            r=p.request
            if r.owner!=owner or r.condition_id not in pairs or (r.yes_token_id,r.no_token_id)!=pairs[r.condition_id]:raise ValueError('merge identity mismatch')
            if p.received_usdc and (p.outcome!=Outcome.SUCCESS or p.received_usdc!=r.quantity or len(p.consumed_positions)!=2):raise ValueError('merge receipt incomplete')
            if p.received_usdc:merges[r.merge_id]=p
    income=_daily_income(config.credentials,wallet,config.day,selected)
    requests,results,merge_requests,merge_results={},{},{},{}
    for kind,p in events:
        if kind=='intent':requests[p['request'].request_id]=p['request']
        elif kind=='order_result':
            requests[p.request.request_id]=p.request;results[p.request.request_id]=p
        elif kind=='merge_intent':merge_requests[p['request'].merge_id]=p['request']
        elif kind=='merge_result':
            merge_requests[p.request.merge_id]=p.request;merge_results[p.request.merge_id]=p
    rows=[]
    for cid in selected:
        row=dict(condition_id=cid,title=next((m['title'] for p in reversed(samples) if p['at']<end and (m:=_market(p,cid,legacy)) and m.get('title')),UNKNOWN),notes=list(income[cid]['notes']))
        row.update({k:UNKNOWN for k in COLUMNS});row.update({k:income[cid][k] for k in ('reward','rebate')})
        try:row['paired_profit'],row['fees']=_gross(tuple(fills.values()),tuple(merges.values()),cid,day)
        except Exception:row['notes'].append('gross PnL: incomplete fill/merge cost provenance')
        for key,r in requests.items():
            if r.quote.condition_id!=cid or r.created_at>=end:continue
            fact=results.get(key)
            complete=(fact is not None and (fact.outcome==Outcome.FAILED and fact.order is None
                or fact.outcome==Outcome.SUCCESS and fact.order is not None and fact.order.authoritative
                and sum((f.quantity for f in fills.values() if f.request_id==key),D('0'))==fact.order.filled_quantity))
            if not complete:
                row['fees']=row['paired_profit']=UNKNOWN
                row['notes'].append('execution: unsettled placement or incomplete owned fill proof')
        for key,r in merge_requests.items():
            if r.condition_id!=cid or r.created_at>=end:continue
            fact=merge_results.get(key)
            if fact is None or not (fact.outcome==Outcome.FAILED or fact.outcome==Outcome.SUCCESS and fact.received_usdc>0):
                row['paired_profit']=UNKNOWN
                row['notes'].append('execution: unsettled merge receipt')
        boundary=[]
        for d in (day,day+timedelta(days=1)):
            facts=[p for p in baselines if p['day']==d.isoformat() and p.get('condition_id',legacy)==cid]
            fact=facts[-1] if facts else {}
            value=fact.get('inventory_pnl')
            valid=isinstance(value,D) and value.is_finite() and fact.get('source') in ('flat','observed_boundary')
            boundary.append(value if valid else None)
        if all(v is not None for v in boundary):row['inventory_delta']=boundary[1]-boundary[0]
        else:row['notes'].append('inventory: missing observed UTC opening/closing boundary; paired unmerged holdings included')
        row['average_occupation']=_average(samples,cid,legacy,start,end)
        if row['average_occupation']==UNKNOWN:row['notes'].append('occupation: missing continuous UTC coverage or per-market evidence')
        if all(isinstance(row[k],D) for k in ('reward','rebate','paired_profit','inventory_delta','fees')):
            row['net_profit']=row['reward']+row['rebate']+row['paired_profit']+row['inventory_delta']-row['fees']
        if isinstance(row['net_profit'],D) and isinstance(row['average_occupation'],D) and row['average_occupation']>0:
            row['yield_pct_per_day']=row['net_profit']/row['average_occupation']*100
        elif row['yield_pct_per_day']==UNKNOWN:row['notes'].append('yield: net or positive average occupation unavailable')
        rows.append(row)
    total=dict(condition_id='TOTAL',title='Total',notes=sorted({note for row in rows for note in row['notes']}))
    for key in COLUMNS:
        total[key]=sum((r[key] for r in rows),D('0')) if all(isinstance(r[key],D) for r in rows) else UNKNOWN
    total['yield_pct_per_day']=total['net_profit']/total['average_occupation']*100 if isinstance(total['net_profit'],D) and isinstance(total['average_occupation'],D) and total['average_occupation']>0 else UNKNOWN
    rows.append(total)
    now=datetime.now(UTC)
    result=dict(day=config.day,as_of=now.isoformat(),rows=rows,safety=_safety(samples,events,start,end,legacy,now),
        provenance=dict(snapshot_seq=snapshot_seq,journal='read-only committed SQLite snapshot (WAL visible)',income='fresh GET for requested UTC date; not cached live income',inventory='next UTC boundary minus opening gross PnL; paired unmerged inventory included',fees='actual owned fill fees, deducted once',unknown_threshold='unset'))
    output_paths(config.state_dir,config.out)
    targets[0].parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    for target in targets:
        fd,temp=tempfile.mkstemp(prefix='.'+target.name+'.',dir=target.parent)
        try:
            with os.fdopen(fd,'w') as f:
                if target.suffix=='.json':json.dump(result,f,default=str,allow_nan=False,indent=2);f.write('\n')
                else:
                    writer=csv.DictWriter(f,fieldnames=('condition_id','title',*COLUMNS,'notes'));writer.writeheader()
                    writer.writerows(dict(r,notes='; '.join(r['notes'])) for r in rows)
                f.flush();os.fsync(f.fileno())
            os.replace(temp,target)
        finally:
            if os.path.exists(temp):os.unlink(temp)
    return result
