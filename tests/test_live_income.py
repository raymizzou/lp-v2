from tests.test_readonly_sdk import step_at
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
import httpx
import pytest
from polymarket.models.clob.rewards import UserEarning
from polymarket.environments import PRODUCTION
from tests.test_live_sdk import AT,CID,WALLET,request,adapter
from tests.fakes.live_exchange import Exchange


def earning(amount='1',cid=CID,date=AT,wallet=WALLET):
    return UserEarning(asset_address=PRODUCTION.collateral_token,asset_rate='1',condition_id=cid,
                       date=int(date.timestamp()*1000),earnings=amount,maker_address=wallet)


def income_world(tmp_path):
    ex=Exchange()
    live,journal=adapter(tmp_path,ex.sdk)
    calls=[]
    def get(req):
        calls.append(req)
        return httpx.Response(200,json=[dict(date=AT.date().isoformat(),condition_id=CID,
            maker_address=WALLET,asset_address=PRODUCTION.collateral_token,rebated_fees_usdc='1')])
    live.http=httpx.Client(transport=httpx.MockTransport(get))
    ex.sdk.list_user_earnings_for_day.return_value=(SimpleNamespace(items=(earning(),)),SimpleNamespace(items=(earning(),earning(cid='0x'+'b'*64))))
    ex.sdk.get_reward_percentages.return_value={CID:25.0}
    ex.sdk.get_order_scoring.return_value=True
    return live,journal,ex,calls


def test_income_reads_utc_market_and_cadence(tmp_path):
    live,journal,ex,calls=income_world(tmp_path)
    live.place_order(request(),at=AT)
    live.read_orders()  # Scoring is measured only after authoritative current-live readback.
    facts=live.read_income(AT)
    assert facts['reward_today']==D('2') and facts['rebate_today']==D('1')
    assert facts['reward_share']==D('25')
    assert facts['scoring']=={'order-1':True}
    assert ex.sdk.list_user_earnings_for_day.call_args.kwargs=={'date':'2026-11-02'}
    assert calls[0].method=='GET' and calls[0].url.path=='/rebates/current'
    assert dict(calls[0].url.params)==dict(date='2026-11-02',maker_address=WALLET)
    live.read_income(AT+timedelta(seconds=59))
    assert len(calls)==1
    live.read_income(AT+timedelta(seconds=60))
    assert len(calls)==2
    ex.sdk.list_user_earnings_for_day.side_effect=ValueError('incomplete page')
    assert live.read_income(AT+timedelta(seconds=120))['reward_today']=='UNKNOWN'
    ex.sdk.list_user_earnings_for_day.side_effect=None
    ex.sdk.list_user_earnings_for_day.side_effect=lambda **kw: (SimpleNamespace(items=(earning(amount='NaN'),)),)
    assert live.read_income(AT+timedelta(seconds=180))['reward_today']=='UNKNOWN'


def test_income_formula_and_unknown(tmp_path):
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.cli import RunConfig
    from lp_v2.models import Fill,Order,OrderResult,OrderStatus,Outcome,Quote,MergeRequest,MergeResult,Position
    from tests.test_live_runtime import inputs,Observations
    live,journal,ex,calls=income_world(tmp_path)
    opening=request()
    # Independent prior-day-baseline and authoritative owned history, all synthetic.
    journal.append('day_baseline',dict(day='2026-11-02',at=AT,inventory_pnl=D('0')))
    journal.append('occupation',dict(at=AT,condition_id=CID,total=D('20')))
    for i,(token,price,qty,fee,at) in enumerate([('101','.40','2','0',AT),('102','.50','2','0',AT),('101','.50','1','.10',AT+timedelta(seconds=2))]):
        r=replace(opening,request_id=f'prior-{i}',quote=Quote(CID,token,D(price),D(qty)))
        journal.append('intent',dict(request=r,hedge_ceiling=D('.98')-D(price)))
        journal.append('order_result',OrderResult(r,Outcome.SUCCESS,Order(f'prior-order-{i}',r,D(qty),D('0'),OrderStatus.FILLED,at,True),at))
        journal.append('fill',Fill(f'prior-fill-{i}',f'prior-order-{i}',r.request_id,r.owner,CID,token,D(qty),D(price),D(fee),at))
    m=MergeRequest('prior-merge',opening.owner,CID,'101','102',D('2'),AT+timedelta(seconds=1))
    consumed=tuple(Position(opening.owner,CID,t,D('2'),D(c),m.created_at) for t,c in [('101','.80'),('102','1.00')])
    journal.append('merge_intent',dict(request=m,cash=D('98.2'),positions=consumed))
    journal.append('merge_result',MergeResult(m,Outcome.SUCCESS,D('2'),m.created_at,consumed))
    ex.quantities={'101':D('1'),'102':D('0')}
    ex.cash=D('99.60')
    p=inputs(yes='.39')
    p=replace(p,books=tuple(replace(b,observed_at=AT+timedelta(seconds=100)) for b in p.books))
    obs=Observations((p,));obs.region={}
    runner=LiveRunner(RunConfig(D('100'),AT),live,obs,clock=lambda:ex.now)
    result=step_at(runner, AT+timedelta(seconds=100))
    fields=result['markets'][0]['m4']
    assert result['schema_version']==1
    assert D(fields['paired_profit'])==D('.20')
    assert D(fields['reward_today'])==2 and D(fields['rebate_today'])==1
    assert D(fields['net_profit'])==D('3.00')
    assert D(fields['net_yield_pct_per_day'])==D('15')
    assert D(fields['lp_yield_pct_per_day'])==D('10')
    # Restart keeps today's baseline and time-weighted occupation.
    restarted=LiveRunner(RunConfig(D('100'),AT+timedelta(seconds=100)),live,obs,clock=lambda:ex.now)
    assert step_at(restarted, AT+timedelta(seconds=100))['markets'][0]['m4']==fields
    ex.sdk.list_user_earnings_for_day.side_effect=ValueError('unavailable')
    missing=step_at(restarted, AT+timedelta(seconds=160))['markets'][0]['m4']
    assert missing['net_profit']=='UNKNOWN'
    assert missing['lp_yield_pct_per_day']=='UNKNOWN'
    # A UTC rollover cannot silently attribute yesterday's realized profit to today.
    next_day=step_at(restarted, AT+timedelta(days=1))['markets'][0]['m4']
    assert D(next_day['paired_profit'])==0
    assert next_day['net_yield_pct_per_day']=='UNKNOWN'


@pytest.mark.parametrize('gap',[10,10.5,12,61,'restart','failed'])
def test_continuous_utc_rollover_keeps_daily_income(tmp_path,gap):
    from datetime import datetime,UTC
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.cli import RunConfig
    from tests.test_live_runtime import inputs,Observations
    live,journal,ex,calls=income_world(tmp_path)
    start=AT+timedelta(hours=23,minutes=58)
    def reward(**kw):
        date=datetime.fromisoformat(kw['date']).replace(tzinfo=UTC)
        return (SimpleNamespace(items=(earning(amount='2',date=date),)),)
    ex.sdk.list_user_earnings_for_day.side_effect=reward
    live.http.close()
    live.http=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=[dict(
        date=req.url.params['date'],condition_id=CID,maker_address=WALLET,
        asset_address=PRODUCTION.collateral_token,rebated_fees_usdc='1')])))
    obs=Observations(())
    def books(at,bid):
        p=inputs(yes=bid)
        obs.plans=(replace(p,books=tuple(replace(b,observed_at=at) for b in p.books)),)
    books(start,'.40')
    runner=LiveRunner(RunConfig(D('100'),start),live,obs,clock=lambda:ex.now)
    ex.reject_tokens.add('102')
    step_at(runner, start)
    ex.fill('order-1','2','.40',at=start+timedelta(seconds=1))
    # This valuation/cadence fixture stays eligible; deny/cancel behavior has its own regression.
    midnight=AT+timedelta(days=1)
    seconds=25 if gap=='failed' else 12 if gap=='restart' else gap
    before=midnight-timedelta(seconds=seconds)
    books(before,'.40')
    step_at(runner, before)
    if gap=='restart':
        runner=LiveRunner(RunConfig(D('100'),midnight),live,obs,clock=lambda:ex.now)
    if gap=='failed':
        original=ex.sdk.list_account_trades.side_effect
        ex.sdk.list_account_trades.side_effect=TimeoutError('unavailable')
        step_at(runner, midnight-timedelta(seconds=12))
        ex.sdk.list_account_trades.side_effect=original
    books(midnight,'.42')
    step_at(runner, midnight)
    baseline=[p for _,p in journal.events('day_baseline') if p['day']=='2026-11-03'][-1]
    observed=gap not in (61,'restart','failed')
    assert baseline['inventory_pnl']==(D('.06') if observed else None)
    assert baseline['at']==midnight
    assert baseline['mark_at']==(midnight if observed else None)
    assert baseline['source']==('observed_boundary' if observed else 'UNKNOWN')
    books(midnight+timedelta(seconds=10),'.43')
    metrics=step_at(runner, midnight+timedelta(seconds=10))['markets'][0]['m4']
    assert D(metrics['reward_today'])==2 and D(metrics['rebate_today'])==1
    assert D(metrics['paired_profit'])==0
    if observed:
        assert D(metrics['net_profit'])==D('3.02')
        assert D(metrics['net_yield_pct_per_day'])==D('30.2')
    else:
        assert metrics['net_profit']==metrics['net_yield_pct_per_day']=='UNKNOWN'


@pytest.mark.parametrize('rollover',[False,True])
def test_failed_round_does_not_republish_old_income_as_current(tmp_path,rollover):
    from lp_v2.live_runtime import LiveRunner
    from lp_v2.cli import RunConfig
    from tests.test_live_runtime import inputs,Observations
    live,journal,ex,calls=income_world(tmp_path)
    obs=Observations((inputs(),))
    runner=LiveRunner(RunConfig(D('100'),AT),live,obs,clock=lambda:ex.now)
    step_at(runner, AT)
    before=step_at(runner, AT+timedelta(seconds=10))
    assert D(before['markets'][0]['m4']['net_profit'])==3
    assert D(before['markets'][0]['m4']['net_yield_pct_per_day'])==30
    ex.sdk.list_account_trades.side_effect=TimeoutError('unavailable')
    when=AT+timedelta(days=1) if rollover else AT+timedelta(seconds=20)
    failed=step_at(runner, when)
    assert failed['as_of']==when.isoformat()
    assert set(failed['markets'][0]['m4'].values())=={'UNKNOWN'}
    assert failed['freshness']['decision_as_of']==(AT+timedelta(seconds=10)).isoformat()
    assert failed['accounting']==dict(status='UNKNOWN',as_of=(AT+timedelta(seconds=10)).isoformat())


def test_scoring_only_current_owned_live_orders(tmp_path):
    from lp_v2.models import Quote
    live,journal,ex,calls=income_world(tmp_path)
    orders={}
    for name in ('live','filled','cancelled','uncertain'):
        r=replace(request(),request_id=name,quote=Quote(CID,'101',D('.40'),D('1')))
        orders[name]=live.place_order(r,at=AT).order.order_id
    ex.fill(orders['filled'],'1','.40')
    live.cancel_order(orders['cancelled'],at=AT+timedelta(seconds=1))
    ex.reject_tokens.add('102')
    live.place_order(replace(request(),request_id='rejected',quote=Quote(CID,'102',D('.50'),D('1'))),at=AT)
    ex.orders['foreign']=ex.orders[orders['live']].model_copy(update={'id':'foreign'})
    original=ex.sdk.get_order.side_effect
    def get(*,order_id):
        if order_id==orders['uncertain']:raise TimeoutError('unavailable')
        return original(order_id=order_id)
    ex.sdk.get_order.side_effect=get
    live.read_orders()
    facts=live.read_income(AT)
    assert facts['scoring']=={orders['live']:True}
    assert [c.kwargs['order_id'] for c in ex.sdk.get_order_scoring.call_args_list]==[orders['live']]
    live.read_income(AT+timedelta(seconds=59))
    assert ex.sdk.get_order_scoring.call_count==1
    ex.terminal(orders['live']);live.read_orders()
    assert live.read_income(AT+timedelta(seconds=60))['scoring']=={}
    assert ex.sdk.get_order_scoring.call_count==1
