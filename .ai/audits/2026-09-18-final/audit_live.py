"""Независимые проверки замороженного живого прогона. Только чтение БД."""
import asyncio
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
import json
import statistics

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from app.config import settings
from app.crud.test_order_rollup import TestOrderRollupCrud

getcontext().prec=60
D=Decimal
UTC=timezone.utc


def kind(b):
    return 'v3' if b['copybot_v3_time_in_minutes'] is not None else 'v2' if b['copybot_v2_time_in_minutes'] is not None else 'v1' if b['copy_bot_min_time_profitability_min'] is not None else 'ordinary'


def distribution(values):
    if not values:return {}
    a=sorted(values)
    return {'count':len(a),'min':a[0],'median':statistics.median(a),'p95':a[int((len(a)-1)*.95)],'max':a[-1]}


async def main():
    assert settings.DB_URL.endswith('/orderflow_live_clean_20260917')
    conn=await asyncpg.connect(settings.DB_URL.replace('+asyncpg',''))
    result={}
    async with conn.transaction(isolation='repeatable_read',readonly=True):
        bots={b['id']:dict(b) for b in await conn.fetch('SELECT * FROM test_bots')}
        orders=[dict(o) for o in await conn.fetch('SELECT * FROM test_orders ORDER BY id')]
        specs={s['symbol']:dict(s) for s in await conn.fetch('SELECT * FROM asset_exchange_specs')}
        symbols=sorted({o['asset_symbol'] for o in orders})
        ticks=[dict(h) for h in await conn.fetch('SELECT id,symbol,event_time,created_at,last_price FROM asset_history WHERE symbol=ANY($1) ORDER BY created_at,id', symbols)]
        rollups=[dict(r) for r in await conn.fetch('SELECT * FROM test_order_rollups')]
        result['database']=dict(await conn.fetchrow('SELECT current_database() AS name, now() AS checked_at, (SELECT count(*) FROM asset_history) AS ticks, (SELECT count(*) FROM watched_pair) AS watched'))
        result['quality']=dict(await conn.fetchrow("SELECT count(*) FILTER (WHERE created_at-event_time>interval '120 seconds') stale_arrivals, max(created_at-event_time) max_arrival_age FROM asset_history"))
        result['duplicates']=dict(await conn.fetchrow('SELECT count(*) groups FROM (SELECT bot_id,open_time,close_time,count(*) FROM test_orders GROUP BY 1,2,3 HAVING count(*)>1) s'))
    await conn.close()
    result['coverage']={'configured':dict(Counter(kind(b) for b in bots.values())), 'traded':dict(Counter(kind(bots[i]) for i in {o['bot_id'] for o in orders})), 'orders':dict(Counter(kind(bots[o['bot_id']]) for o in orders)), 'ma_configured':sum(bool(b['consider_ma_for_open_order'] or b['consider_ma_for_close_order']) for b in bots.values()),'volatile_configured':sum(bool(b['min_timeframe_asset_volatility']) for b in bots.values()),'symbols':symbols}
    checks=Counter(); by_kind=defaultdict(lambda:{'orders':0,'positive':0,'pnl':D(0),'fee':D(0)}); samples=defaultdict(list)
    arrivals=defaultdict(list)
    for h in ticks:arrivals[h['symbol']].append(h)
    keys={s:[h['created_at'] for h in rows] for s,rows in arrivals.items()}
    ages=defaultdict(list)
    reason_counts=Counter(); donor_pairs=Counter(); parameter_mismatches=[]
    for o in orders:
        b=bots[o['bot_id']]; k=kind(b); spec=specs[o['asset_symbol']]
        filters={f['filterType']:f for f in json.loads(spec['filters'])}
        step=D(filters['PRICE_FILTER']['tickSize']); rate=spec['taker_commission_rate'] or D('.0005')
        amount=o['balance']/o['open_price']; sign=1 if o['order_type']=='BUY' else -1
        pnl=sign*amount*(o['close_price']-o['open_price'])-o['open_fee']-o['close_fee']
        checks['pnl_mismatch']+=abs(pnl-o['profit_loss'])>D('1e-8')
        checks['fee_mismatch']+=abs(amount*o['open_price']*rate-o['open_fee'])>D('1e-8') or abs(amount*o['close_price']*rate-o['close_fee'])>D('1e-8')
        checks['invalid']+=o['open_price']<=0 or o['close_price']<=0 or o['balance']<=0 or o['close_time']<o['open_time']
        checks['sl_formula_mismatch']+=o['stop_loss_price'] != o['open_price']-sign*o['stop_loss_ticks']*step
        duration=(o['close_time']-o['open_time']).total_seconds()
        checks['time_exit_before_30s']+=o['stop_reason_event']=='stop-long-lose' and duration<29.9
        reason_counts[k+':'+str(o['stop_reason_event'])]+=1
        cnl=o['open_price']*(1+sign*rate)/(1-sign*rate)
        if o['stop_reason_event']=='stop-long-lose':
            checks['time_exit_without_low_gain']+=sign*(o['close_price']-cnl)/step>=10
        source_bot=bots.get(o['referral_bot_id']) or b
        if o['stop_reason_event']=='stop-won':
            checks['profit_exit_below_breakeven']+=sign*(o['close_price']-cnl)<=0
            if not source_bot['use_trailing_stop']:
                target=(o['open_price']*(1+sign*rate)+sign*o['stop_success_ticks']*step)/(1-sign*rate)
                checks['fixed_profit_before_target']+=sign*(o['close_price']-target)<-step/2
            else:
                hs=arrivals[o['asset_symbol']]; ks=keys[o['asset_symbol']]
                a=bisect_right(ks,o['open_time']);z=bisect_right(ks,o['close_time'])
                prices=[o['open_price']]+[h['last_price'] for h in hs[a:z]]
                peak=max(prices) if sign==1 else min(prices)
                checks['trailing_profit_without_pullback']+=sign*(peak-o['close_price'])<o['stop_success_ticks']*step-step/2
        stats=by_kind[k];stats['orders']+=1;stats['positive']+=o['profit_loss']>0;stats['pnl']+=o['profit_loss'];stats['fee']+=o['open_fee']+o['close_fee']
        config=b
        if o['referral_bot_id'] is not None:
            config=bots[o['referral_bot_id']]
            donor_pairs[f"{o['bot_id']}->{o['referral_bot_id']}"]+=1
            checks['invalid_v1_donor_kind']+=k=='v1' and kind(config)!='ordinary'
        for field in ('start_updown_ticks','stop_loss_ticks','stop_success_ticks'):
            if o[field]!=config[field]:parameter_mismatches.append({'order':o['id'],'field':field,'order_value':o[field],'donor_value':config[field]})
        for phase in ('open','close'):
            moment=o[phase+'_time']; price=o[phase+'_price']; hs=arrivals[o['asset_symbol']]; ix=bisect_right(keys[o['asset_symbol']],moment)-1
            h=hs[ix] if ix>=0 else None
            if h is None:
                checks[phase+'_missing_history']+=1;continue
            ages[phase].append((moment-h['event_time']).total_seconds())
            if h['last_price']!=price:
                checks[phase+'_different_latest']+=1
                if len(samples[phase])<10:samples[phase].append({'order':o['id'],'price':price,'latest':h['last_price'],'time':moment,'tick_received':h['created_at']})
            matching=next((t for t in reversed(hs[max(0,ix-100):ix+1]) if t['last_price']==price and -5<=(moment-t['event_time']).total_seconds()<120),None)
            checks[phase+'_no_recent_matching_tick']+=matching is None
    result.update(checks=dict(checks),stats=dict(by_kind),reasons=dict(reason_counts),price_age={p:distribution(a) for p,a in ages.items()},latest_price_mismatch_samples=dict(samples),donor_pairs=dict(donor_pairs),parameter_mismatches=parameter_mismatches[:20],parameter_mismatch_count=len(parameter_mismatches))
    result['feed']={s:{'ticks':len(hs),'arrival_gap_seconds':distribution([(b['created_at']-a['created_at']).total_seconds() for a,b in zip(hs,hs[1:])]),'unchanged_prices':sum(a['last_price']==b['last_price'] for a,b in zip(hs,hs[1:]))} for s,hs in arrivals.items()}
    # Свёртки сверяем с сырыми строками по полному ключу, не через код построения.
    expected=defaultdict(lambda:[0,0,D(0),D(0),0,0,0])
    last=max((r['bucket_start'] for r in rollups),default=None)
    watermark=last+timedelta(minutes=settings.ROLLUP_BUCKET_MINUTES) if last else None
    for o in orders:
        if watermark and o['created_at']<watermark:
            stamp=o['created_at']; bucket=datetime.fromtimestamp(int(stamp.timestamp())//600*600,UTC)
            key=(bucket,o['bot_id'],o['referral_bot_id'],o['asset_symbol']);r=expected[key]
            r[0]+=1;r[1]+=o['profit_loss']>0;r[2]+=o['profit_loss'];r[3]+=o['open_fee']+o['close_fee']
            for i,reason in enumerate(('stop-won','stop-loosed','stop-long-lose'),4):r[i]+=o['stop_reason_event']==reason
    actual={(r['bucket_start'],r['bot_id'],r['referral_bot_id'],r['asset_symbol']):[r[f] for f in ('orders_count','profitable_count','profit_loss_sum','fee_sum','stop_won_count','stop_loosed_count','stop_long_lose_count')] for r in rollups}
    result['rollups']={'rows':len(actual),'expected_rows':len(expected),'watermark':watermark,'mismatched_keys':sum(actual.get(k)!=expected.get(k) for k in set(actual)|set(expected))}
    # Сам отчёт проверяем через его API, ожидание считаем из полного сырья.
    engine=create_async_engine(settings.DB_URL)
    comparisons=[]
    async with AsyncSession(engine) as session:
        await session.execute(text('SET TRANSACTION READ ONLY'))
        crud=TestOrderRollupCrud(session)
        end=max(o['created_at'] for o in orders)
        for hours in (1,6,12,24):
            for label,flags in [('all',{}),('ordinary',{'just_not_copy_bots':True}),('v1',{'just_copy_bots':True}),('v2',{'just_copy_bots_v2':True}),('v3',{'just_copy_bots_v3':True})]:
                rows,start,mark=await crud.profit_by_bot(end-timedelta(hours=hours),**flags)
                wanted=defaultdict(lambda:[0,D(0),D(0)])
                for o in orders:
                    if o['created_at']>=start and (label=='all' or kind(bots[o['bot_id']])==label):
                        v=wanted[o['bot_id']];v[0]+=1;v[1]+=o['profit_loss'];v[2]+=o['open_fee']+o['close_fee']
                got={r.bot_id:[int(r.orders_count),r.profit_loss,r.fee] for r in rows}
                mismatches=sum(got.get(k)!=wanted.get(k) for k in set(got)|set(wanted))
                comparisons.append({'hours':hours,'kind':label,'rows':len(rows),'mismatches':mismatches,'effective_start':start})
    await engine.dispose()
    result['report_comparisons']=comparisons
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))


asyncio.run(main())
