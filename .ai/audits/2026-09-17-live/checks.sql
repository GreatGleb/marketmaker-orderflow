-- Только чтение. Окно отделяет текущий прогон от старых сделок.
\set ON_ERROR_STOP on
\if :{?started}
\else
\set started '2026-09-17 18:46:45+00'
\endif
BEGIN READ ONLY;
SET LOCAL statement_timeout = '20s';

SELECT now() AS measured_at;

-- Проверяем время поступления: фильтр только по event_time скрывает старые ответы.
SELECT count(*) AS received_ticks,
       count(*) FILTER (WHERE created_at - event_time > interval '120 seconds') AS stale_arrivals,
       max(extract(epoch FROM created_at - event_time)) AS max_arrival_age_seconds
FROM asset_history WHERE created_at >= :'started';

WITH ordered AS (
 SELECT symbol, event_time,
        lag(event_time) OVER (PARTITION BY symbol ORDER BY id) AS previous_event
 FROM asset_history WHERE created_at >= :'started'
)
SELECT count(*) FILTER (WHERE event_time <= previous_event) AS backwards_or_duplicate_ticks
FROM ordered;

SELECT source, count(*) AS ticks, count(DISTINCT symbol) AS symbols,
       max(event_time) AS latest_tick, now() - max(event_time) AS age
FROM asset_history WHERE event_time >= now() - interval '1 minute'
GROUP BY source;

SELECT CASE WHEN b.copybot_v3_time_in_minutes IS NOT NULL THEN 'v3'
            WHEN b.copybot_v2_time_in_minutes IS NOT NULL THEN 'v2'
            WHEN b.copy_bot_min_time_profitability_min IS NOT NULL THEN 'v1'
            ELSE 'ordinary' END AS kind,
       count(*) AS orders, count(DISTINCT o.bot_id) AS bots,
       min(o.open_time) AS first_open, max(o.close_time) AS last_close,
       sum(o.profit_loss) AS pnl
FROM test_orders o JOIN test_bots b ON b.id = o.bot_id
WHERE o.open_time >= :'started'
GROUP BY 1 ORDER BY 1;

SELECT order_type, stop_reason_event, count(*) AS orders,
       round(avg(extract(epoch FROM close_time - open_time))::numeric, 3)
         AS avg_seconds
FROM test_orders WHERE open_time >= :'started'
GROUP BY 1, 2 ORDER BY 1, 2;

-- Независимое тождество результата: изменение стоимости позиции минус
-- записанные комиссии. Не вызывает PriceCalculator из приложения.
WITH recalculated AS (
  SELECT *,
    CASE order_type WHEN 'BUY' THEN 1 WHEN 'SELL' THEN -1 END
      * balance / NULLIF(open_price, 0) * (close_price - open_price)
      - open_fee - close_fee AS expected_pnl
  FROM test_orders WHERE open_time >= :'started'
)
SELECT count(*) AS checked,
       count(*) FILTER (WHERE abs(profit_loss - expected_pnl) > 0.00000001)
         AS pnl_mismatches,
       count(*) FILTER (WHERE open_price <= 0 OR close_price <= 0
         OR balance <= 0 OR close_time < open_time OR expected_pnl IS NULL)
         AS invalid_rows,
       max(abs(profit_loss - expected_pnl)) AS max_pnl_error
FROM recalculated;

SELECT o.asset_symbol, e.taker_commission_rate,
       count(*) AS checked,
       count(*) FILTER (WHERE abs(o.open_fee - o.balance *
         COALESCE(e.taker_commission_rate, 0.0005)) > 0.00000001
         OR abs(o.close_fee - o.balance / NULLIF(o.open_price, 0)
         * o.close_price * COALESCE(e.taker_commission_rate, 0.0005))
         > 0.00000001) AS fee_mismatches
FROM test_orders o JOIN asset_exchange_specs e ON e.symbol = o.asset_symbol
WHERE o.open_time >= :'started'
GROUP BY 1, 2;

SELECT state, wait_event_type, count(*) AS connections,
       max(now() - xact_start) AS longest_transaction
FROM pg_stat_activity WHERE datname = current_database()
  AND pid <> pg_backend_pid()
GROUP BY 1, 2 ORDER BY 1, 2;

SELECT count(*) AS rollups, max(bucket_start) AS latest_bucket
FROM test_order_rollups;
COMMIT;
