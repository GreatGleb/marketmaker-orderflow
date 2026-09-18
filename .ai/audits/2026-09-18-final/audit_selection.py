"""Сравнение SQL-отбора v1 с независимым расчётом по финальному сырью."""
import asyncio,json
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from decimal import Decimal,getcontext
from unittest.mock import patch
import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine,AsyncSession
from app.config import settings
from app.crud.test_bot import TestBotCrud
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand,WindowCache

getcontext().prec=60
ANCHOR=datetime(2026,9,18,15,49,49,tzinfo=timezone.utc)
class FrozenDateTime(datetime):
 @classmethod
 def now(cls,tz=None):return ANCHOR if tz else ANCHOR.replace(tzinfo=None)

async def main():
 assert settings.DB_URL.endswith('/orderflow_live_clean_20260917')
 conn=await asyncpg.connect(settings.DB_URL.replace('+asyncpg',''))
 async with conn.transaction(readonly=True):
  bots={b['id']:dict(b) for b in await conn.fetch('SELECT * FROM test_bots')}
  orders=[dict(o) for o in await conn.fetch('SELECT * FROM test_orders')]
 await conn.close()
 ordinary={b['id'] for b in bots.values() if b['is_active'] and all(b[f] is None for f in ('copy_bot_min_time_profitability_min','copybot_v2_time_in_minutes','copybot_v3_time_in_minutes'))}
 def sums(minutes,referral=False):
  totals=defaultdict(Decimal)
  for o in orders:
   key=o['referral_bot_id'] if referral else o['bot_id']
   if key in ordinary and o['created_at']>=ANCHOR-timedelta(minutes=float(minutes)):totals[key]+=o['profit_loss']
  return totals
 daily=sums(1440);losing={k for k,v in sums(1440,True).items() if v<0}
 engine=create_async_engine(settings.DB_URL);rows=[]
 async with AsyncSession(engine) as session:
  await session.execute(text('SET TRANSACTION READ ONLY'))
  crud=TestBotCrud(session);cache=WindowCache()
  with patch('app.crud.test_bot.datetime',FrozenDateTime):
   for b in bots.values():
    if b['copy_bot_min_time_profitability_min'] is None:continue
    tf=b['copy_bot_min_time_profitability_min'];totals=sums(tf)
    expected={k for k,v in totals.items() if v>0}
    if b['copybot_v1_check_for_24h_profitability']:expected={k for k in expected if daily.get(k,0)>0}
    if b['copybot_v1_exclude_losing_donors']:expected-=losing
    got=await ProfitableBotUpdaterCommand.filter_profitable_bots_id(crud,tf,b['copybot_v1_check_for_24h_profitability'],b['copybot_v1_exclude_losing_donors'],cache)
    ordered=all(totals[a]>=totals[z] for a,z in zip(got,got[1:]))
    rows.append({'bot':b['id'],'minutes':str(tf),'candidates':len(got),'set_matches':set(got)==expected,'order_matches':ordered})
 await engine.dispose()
 print(json.dumps({'at':str(ANCHOR),'checked':len(rows),'failures':[r for r in rows if not r['set_matches'] or not r['order_matches']],'results':rows},ensure_ascii=False,indent=2))
asyncio.run(main())
