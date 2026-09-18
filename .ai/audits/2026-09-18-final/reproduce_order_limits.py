"""Проверка допуска объёма v3 по сохранённым фильтрам живой пары; без ордеров."""
import asyncio,json
from decimal import Decimal
import asyncpg
from app.config import settings
from app.bots.demo_test_bot import StartTestBotsCommand
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud

async def main():
 assert settings.DB_URL.endswith('/orderflow_live_clean_20260917')
 c=await asyncpg.connect(settings.DB_URL.replace('+asyncpg',''))
 async with c.transaction(readonly=True):
  filters=json.loads(await c.fetchval("SELECT filters FROM asset_exchange_specs WHERE symbol='BMTUSDT' LIMIT 1"))
  price=await c.fetchval("SELECT last_price FROM asset_history WHERE symbol='BMTUSDT' ORDER BY id DESC LIMIT 1")
 await c.close()
 market=AssetExchangeSpecCrud.extract_step_sizes(filters)
 fs={f['filterType']:f for f in filters}
 minimum=Decimal(fs['MIN_NOTIONAL']['notional'])
 rows=[]
 for balance in [Decimal('10'),Decimal('5'),Decimal('4'),Decimal('1')]:
  q,n,refusal=StartTestBotsCommand.compound_order_size(balance,price,market)
  rows.append({'balance':balance,'price':price,'quantity':q,'notional':n,'minimum_notional':minimum,'refusal':str(refusal) if refusal else None,'accepted_below_minimum':refusal is None and n<minimum})
 # Отличающиеся ограничения MARKET_LOT_SIZE проверяем на сохранённых спеках.
 c=await asyncpg.connect(settings.DB_URL.replace('+asyncpg',''))
 async with c.transaction(readonly=True):
  specs=await c.fetch('SELECT symbol,filters FROM asset_exchange_specs')
 examples=[]
 for spec in specs:
  fl=json.loads(spec['filters']);f={v['filterType']:v for v in fl}
  if 'LOT_SIZE' not in f or 'MARKET_LOT_SIZE' not in f:continue
  lot,mlot=f['LOT_SIZE'],f['MARKET_LOT_SIZE']
  if Decimal(mlot['maxQty'])>0 and Decimal(mlot['maxQty'])<Decimal(lot['maxQty']):
   # Объём чуть выше рыночного максимума, но ещё ниже LOT_SIZE.
   q_target=Decimal(mlot['maxQty'])+Decimal(lot['stepSize'])
   md=AssetExchangeSpecCrud.extract_step_sizes(fl)
   q,n,refusal=StartTestBotsCommand.compound_order_size(q_target/Decimal('.99'),Decimal('1'),md)
   if refusal is None and q>Decimal(mlot['maxQty']):
    examples.append({'symbol':spec['symbol'],'quantity':q,'market_max':mlot['maxQty'],'lot_max':lot['maxQty'],'refusal':None})
 await c.close()
 print(json.dumps({'live_pair':rows,'market_max_counterexamples':examples[:5],'market_max_counterexample_count':len(examples)},ensure_ascii=False,indent=2,default=str))
asyncio.run(main())
