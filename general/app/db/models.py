from decimal import Decimal
from typing import Optional

from datetime import datetime

from sqlalchemy import func, types, ForeignKey, Index, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
    declarative_base,
)

Base = declarative_base()


class BaseId(Base):
    __abstract__ = True

    id: Mapped[int] = mapped_column(
        types.Integer, primary_key=True, nullable=False, autoincrement=True
    )
    created_at: Mapped[datetime] = mapped_column(
        types.DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="Date and time of create",
    )
    updated_at: Mapped[datetime] = mapped_column(
        types.DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Date and time of update",
    )


class BigId(BaseId):
    """`id` для таблиц, в которые пишет поток.

    int4 кончается на 2 147 483 647. На полной скорости парк пишет около
    490 строк в секунду в один только `test_orders` — это 42 миллиона строк
    в сутки и переполнение последовательности примерно через 51 день, после
    чего вставки начинают падать. Чистка от этого не спасает: `DELETE` не
    откатывает sequence, та считает выданные значения, а не живые строки.

    Расширить колонку на уже наполненной таблице дорого: `ALTER COLUMN ...
    TYPE` переписывает её целиком под `ACCESS EXCLUSIVE` и требует свободного
    места в размер таблицы с индексами — на заполненном диске так нельзя. На
    пустой таблице это бесплатно, поэтому тип объявлен здесь: новые
    развёртывания получают bigint сразу и переписывать им нечего.
    """

    __abstract__ = True

    id: Mapped[int] = mapped_column(
        types.BigInteger, primary_key=True, nullable=False, autoincrement=True
    )


class AssetPair(BaseId):
    __tablename__ = "asset_pairs"

    pair: Mapped[str] = mapped_column(
        types.String(255),
        nullable=False,
        comment="Trading pair, e.g., BTCUSDT",
    )
    base_asset: Mapped[str] = mapped_column(
        types.String(255),
        nullable=False,
        comment="Base asset in the trading pair",
    )
    quote_asset: Mapped[str] = mapped_column(
        types.String(255),
        nullable=False,
        comment="Quote asset in the trading pair",
    )


class AssetExchangeSpec(BaseId):
    __tablename__ = "asset_exchange_specs"

    source: Mapped[str] = mapped_column(
        types.String(255),
        nullable=False,
        server_default="BINANCE",
        comment="Source exchange",
    )

    asset_pairs_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("asset_pairs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Foreign key to asset_pairs",
    )

    asset_pair: Mapped[Optional[AssetPair]] = relationship(
        backref="exchange_specs", lazy="joined"
    )

    contract_type: Mapped[str] = mapped_column(
        types.String(255), nullable=False, comment="Contract type"
    )

    symbol: Mapped[str] = mapped_column(
        types.String(255), nullable=False, comment="Symbol"
    )

    delivery_date: Mapped[Optional[datetime]] = mapped_column(
        types.DateTime, nullable=True, comment="Delivery date"
    )

    onboard_date: Mapped[Optional[datetime]] = mapped_column(
        types.DateTime, nullable=True, comment="Onboard date"
    )

    status: Mapped[Optional[str]] = mapped_column(
        types.String(50), nullable=True, comment="Status"
    )

    maint_margin_percent: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Maintenance margin percent"
    )

    required_margin_percent: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Required margin percent"
    )

    base_asset: Mapped[Optional[str]] = mapped_column(
        types.String(50), nullable=True, comment="Base asset"
    )

    quote_asset: Mapped[Optional[str]] = mapped_column(
        types.String(50), nullable=True, comment="Quote asset"
    )

    margin_asset: Mapped[Optional[str]] = mapped_column(
        types.String(50), nullable=True, comment="Margin asset"
    )

    price_precision: Mapped[Optional[int]] = mapped_column(
        types.Integer, nullable=True, comment="Price precision"
    )

    quantity_precision: Mapped[Optional[int]] = mapped_column(
        types.Integer, nullable=True, comment="Quantity precision"
    )

    base_asset_precision: Mapped[Optional[int]] = mapped_column(
        types.Integer, nullable=True, comment="Base asset precision"
    )

    quote_precision: Mapped[Optional[int]] = mapped_column(
        types.Integer, nullable=True, comment="Quote precision"
    )

    underlying_type: Mapped[Optional[str]] = mapped_column(
        types.String(50), nullable=True, comment="Underlying type"
    )

    underlying_sub_type: Mapped[Optional[dict]] = mapped_column(
        types.JSON, nullable=True, comment="Underlying sub-type"
    )

    settle_plan: Mapped[Optional[int]] = mapped_column(
        types.Integer, nullable=True, comment="Settle plan"
    )

    trigger_protect: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Trigger protect"
    )

    filters: Mapped[Optional[dict]] = mapped_column(
        types.JSON, nullable=True, comment="Filters"
    )

    order_type: Mapped[Optional[dict]] = mapped_column(
        types.JSON, nullable=True, comment="Order type"
    )

    time_in_force: Mapped[Optional[dict]] = mapped_column(
        types.JSON, nullable=True, comment="Time in force"
    )

    liquidation_fee: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Liquidation fee"
    )

    market_take_bound: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Market take bound"
    )

    maker_commission_rate: Mapped[Optional[Decimal]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="Maker commission rate for this symbol (0.0002 = 0.02%)",
    )

    taker_commission_rate: Mapped[Optional[Decimal]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="Taker commission rate for this symbol (0.0004 = 0.04%)",
    )


class AssetHistory(BigId):
    __tablename__ = "asset_history"

    asset_exchange_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("asset_exchange_specs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Foreign key to asset_exchange_specs",
    )

    asset_exchange: Mapped[Optional[AssetExchangeSpec]] = relationship(
        backref="asset_history", lazy="joined"
    )

    symbol: Mapped[str] = mapped_column(
        types.String(255), nullable=False, comment="Symbol (s)"
    )

    source: Mapped[str] = mapped_column(
        types.String(255),
        nullable=False,
        server_default="BINANCE",
        comment="Source exchange",
    )

    last_price: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Last price (c)"
    )

    price_change_24h: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Price change in 24h (p)"
    )

    price_change_percent_24h: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Price change percent in 24h (P)"
    )

    base_asset_volume_24h: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Base asset volume in 24h (v)"
    )

    quote_asset_volume_24h: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Quote asset volume in 24h (q)"
    )

    weighted_avg_price_24h: Mapped[Optional[float]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="Weighted average price in 24h (w)",
    )

    price_high_24h: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="24h high price (h)"
    )

    price_low_24h: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="24h low price (l)"
    )

    event_time: Mapped[datetime] = mapped_column(
        types.DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="Event time (E)",
    )

    statistics_open_time: Mapped[Optional[int]] = mapped_column(
        types.BigInteger, nullable=True, comment="Statistics open time (O)"
    )

    statistics_close_time: Mapped[Optional[int]] = mapped_column(
        types.BigInteger, nullable=True, comment="Statistics close time (C)"
    )

    # Composite index for symbol, event_time, and last_price
    __table_args__ = (
        Index(
            "idx_assethistory_symbol_event_price",
            "symbol",
            "event_time",
            "last_price",
        ),
    )


class AssetVolumeVolatility(BaseId):
    __tablename__ = "asset_volume_volatility"

    asset_exchange_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("asset_exchange_specs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Foreign key to asset_exchange_specs",
    )

    asset_exchange: Mapped[Optional[AssetExchangeSpec]] = relationship(
        backref="volume_volatility_history", lazy="joined"
    )

    volume_24h_base: Mapped[float] = mapped_column(
        types.Numeric, nullable=False, comment="Base asset 24h volume"
    )

    volume_24h_quote: Mapped[float] = mapped_column(
        types.Numeric, nullable=False, comment="Quote asset 24h volume"
    )

    weighted_avg_price_24h: Mapped[float] = mapped_column(
        types.Numeric, nullable=False, comment="Weighted average price in 24h"
    )

    price_high_24h: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="High 24h price"
    )

    price_low_24h: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Low 24h price"
    )

    volatility_percentage: Mapped[float] = mapped_column(
        types.Numeric, nullable=False, comment="Volatility percentage"
    )


class StrategyPair(BaseId):
    """Пара, нужная конкретной стратегии.

    `watched_pair` остаётся общим техническим списком питателя и
    собирается как объединение этих наборов: один поток котировок на
    всех, независимые наборы у каждой стратегии. Иначе пересборка пар
    одной стратегии молча лишала бы котировок ботов другой.

    Один инструмент может принадлежать нескольким стратегиям — отсюда
    уникальность по паре полей, а не по одному `asset_exchange_id`.
    """

    __tablename__ = "strategy_pairs"

    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Чей это набор пар",
    )
    asset_exchange_id: Mapped[int] = mapped_column(
        ForeignKey("asset_exchange_specs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Инструмент из справочника",
    )

    __table_args__ = (
        Index(
            "uq_strategy_pairs_key",
            "strategy_id",
            "asset_exchange_id",
            unique=True,
        ),
    )


class WatchedPair(BaseId):
    __tablename__ = "watched_pair"

    asset_exchange_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("asset_exchange_specs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Foreign key to asset_exchange_specs",
    )

    asset_exchange: Mapped[Optional[AssetExchangeSpec]] = relationship(
        backref="watched_pairs", lazy="joined"
    )


class AssetOrderBook(BigId):
    __tablename__ = "asset_order_book"

    asset_exchange_id: Mapped[int] = mapped_column(
        ForeignKey("asset_exchange_specs.id"), nullable=False
    )
    asset_exchange: Mapped[Optional[AssetExchangeSpec]] = relationship(
        backref="asset_order_books", lazy="joined"
    )

    transaction_time: Mapped[int] = mapped_column(
        types.BigInteger, comment="Transaction time from Binance (T)"
    )

    bids: Mapped[list] = mapped_column(
        types.JSON, nullable=False, comment="List of bid [price, quantity]"
    )

    asks: Mapped[list] = mapped_column(
        types.JSON, nullable=False, comment="List of ask [price, quantity]"
    )


class TestOrder(BigId):
    __tablename__ = "test_orders"

    asset_symbol: Mapped[str] = mapped_column(
        nullable=False, index=True, comment="Trading symbol like BTCUSDT"
    )

    balance: Mapped[float] = mapped_column(
        types.Numeric, nullable=False, comment="Starting balance for the order"
    )
    order_type: Mapped[str] = mapped_column(
        nullable=False, comment="Order type: buy/sell"
    )

    open_price: Mapped[float] = mapped_column(
        types.Numeric, nullable=False, comment="Opening price of the asset"
    )
    open_time: Mapped[datetime] = mapped_column(
        types.DateTime(timezone=True),
        default=func.now(),
        nullable=False,
        comment="Time of order open",
    )
    open_fee: Mapped[float] = mapped_column(
        types.Numeric,
        nullable=False,
        comment="Fee charged at order open (0.02%)",
    )

    stop_loss_price: Mapped[float] = mapped_column(
        types.Numeric, nullable=False, comment="Stop-loss price"
    )

    close_price: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Closing price"
    )
    close_time: Mapped[Optional[datetime]] = mapped_column(
        types.DateTime(timezone=True),
        nullable=True,
        comment="Time of order close",
    )
    close_fee: Mapped[Optional[float]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="Fee charged at order close (0.05%)",
    )

    profit_loss: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Profit or loss after all fees"
    )
    is_active: Mapped[bool] = mapped_column(
        default=True,
        nullable=False,
        comment="Whether the order is still active",
    )
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("test_bots.id"), nullable=True
    )
    stop_loss_ticks = mapped_column(
        types.Integer,
        default=0,
        server_default="0",
        nullable=False,
        comment="Stop ticks",
    )
    stop_success_ticks: Mapped[int] = mapped_column(
        types.Integer,
        nullable=False,
        server_default="0",
        default=10,
        comment="Target Profit/Close in Ticks",
    )
    start_updown_ticks: Mapped[int] = mapped_column(
        types.Integer,
        default=0,
        server_default="0",
        nullable=False,
        comment="Start updown ticks",
    )
    stop_reason_event: Mapped[str] = mapped_column(
        nullable=True,
        comment="Reason event of stopping order",
    )
    referral_bot_id: Mapped[int] = mapped_column(
        ForeignKey("test_bots.id"), nullable=True
    )
    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id"),
        nullable=False,
        comment="Стратегия парка, которому принадлежит бот",
    )
    executed_strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id"),
        nullable=False,
        comment=(
            "Стратегия, по которой сделка фактически исполнена. У обычного "
            "бота совпадает со strategy_id, у копибота — стратегия "
            "конечного донора"
        ),
    )
    algorithm_version: Mapped[Optional[str]] = mapped_column(
        types.String,
        nullable=True,
        comment=(
            "Версия алгоритма на момент открытия позиции; NULL у сделок, "
            "записанных до появления поля"
        ),
    )
    donor_chain: Mapped[Optional[list]] = mapped_column(
        # none_as_null: иначе SQLAlchemy кладёт в JSONB скаляр `null`, а
        # не SQL NULL. Отличить такую строку от «цепочки нет» можно
        # только сравнением с 'null'::jsonb — то есть `IS NULL` в любом
        # отчёте молча считает её заполненной.
        JSONB(none_as_null=True),
        nullable=True,
        comment=(
            "Цепочка id доноров от копибота к обычному боту, например "
            "[v2_id, v1_id]; NULL у обычных ботов"
        ),
    )
    # Индексов на новых колонках нет намеренно: в `test_orders` идёт поток
    # около 490 строк в секунду, и каждый лишний индекс — это работа на
    # каждой вставке. Отчёты по стратегиям читают свёртки, а там свои
    # индексы на куда меньшем объёме.


class Strategy(BaseId):
    """Торговая стратегия: алгоритм входа/выхода и его настройки.

    Строка здесь — не реализация, а её регистрация. Сам алгоритм
    выбирается из реестра Python по `key`; неизвестный ключ означает, что
    бот этой стратегии запускаться не должен, а не что его надо молча
    провести по текущему алгоритму.
    """

    __tablename__ = "strategies"

    key: Mapped[str] = mapped_column(
        types.String,
        nullable=False,
        unique=True,
        comment="Технический ключ: legacy, strategy_0",
    )
    title: Mapped[str] = mapped_column(
        types.String, nullable=False, comment="Человекочитаемое название"
    )
    pair_policy: Mapped[str] = mapped_column(
        types.String,
        nullable=False,
        server_default="manual",
        comment=(
            "Как стратегия набирает пары: manual — явный список, "
            "volatility_jumps — отбор по скачкам, как у legacy"
        ),
    )
    allows_new_entries: Mapped[bool] = mapped_column(
        types.Boolean,
        nullable=False,
        server_default="true",
        default=True,
        comment=(
            "Разрешены ли новые входы. Снятый признак не закрывает уже "
            "открытые позиции: их доводит тот же алгоритм, с которым они "
            "открывались"
        ),
    )


class TestBot(BaseId):
    __tablename__ = "test_bots"

    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id"),
        nullable=False,
        index=True,
        comment="Стратегия, экземпляром которой является бот",
    )
    bot_kind: Mapped[str] = mapped_column(
        types.String,
        nullable=False,
        server_default="ordinary",
        index=True,
        comment=(
            "Вид бота: ordinary, copy_v1, copy_v2, copy_v3. Колонки-маркеры "
            "остались окнами оценки прибыльности, а не признаком типа"
        ),
    )
    strategy_config: Mapped[Optional[dict]] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
        comment=(
            "Настройки, специфичные для алгоритма стратегии, с ключом "
            "schema_version. NULL — своих настроек у стратегии нет"
        ),
    )
    donor_scope: Mapped[Optional[dict]] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
        comment=(
            "Пул доноров копибота: {'mode': 'all'} либо "
            "{'mode': 'list', 'strategies': ['legacy']}. NULL у обычных "
            "ботов и означает отсутствие ограничений"
        ),
    )
    symbol: Mapped[str] = mapped_column(nullable=False)
    balance: Mapped[float] = mapped_column(
        types.Numeric(precision=20, scale=10),
        nullable=False,
        comment="Balance for trading",
    )
    stop_success_ticks: Mapped[Optional[int]] = mapped_column(
        types.Integer,
        nullable=True,
        comment="Target Profit/Close in Ticks",
    )
    stop_loss_ticks: Mapped[Optional[int]] = mapped_column(
        types.Integer, nullable=True, comment="Stop-loss in ticks"
    )
    is_active: Mapped[bool] = mapped_column(
        types.Boolean, default=True, nullable=False, comment="Is active bot"
    )
    start_updown_ticks: Mapped[Optional[int]] = mapped_column(
        types.Integer,
        default=0,
        nullable=True,
        comment="Start updown ticks",
    )
    min_timeframe_asset_volatility: Mapped[float] = mapped_column(
        types.Numeric(precision=10, scale=2),
        nullable=True,
        comment="Duration of the time window (in minutes) over which asset volatility is measured",
    )
    copy_bot_min_time_profitability_min: Mapped[float] = mapped_column(
        types.Numeric(precision=10, scale=2),
        nullable=True,
        comment="For copy bot the minimum time it takes for the original bot being monitored to be profitable",
    )
    time_to_wait_for_entry_price_to_open_order_in_seconds: Mapped[float] = (
        mapped_column(
            types.Numeric(precision=10, scale=2),
            nullable=True,
            comment="The maximum duration (in minutes) a bot will wait for the entry price to be reached before attempting to open an order",
        )
    )
    stop_win_percents: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Stop win percentage"
    )
    stop_loss_percents: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Stop loss percentage"
    )
    start_updown_percents: Mapped[Optional[float]] = mapped_column(
        types.Numeric, nullable=True, comment="Start up/down percentage"
    )
    consider_ma_for_open_order: Mapped[bool] = mapped_column(
        types.Boolean,
        default=False,
        comment="Whether to consider the moving average (MA) when opening the order",
    )
    consider_ma_for_close_order: Mapped[bool] = mapped_column(
        types.Boolean,
        default=False,
        comment="Whether to consider the moving average (MA) when closing the order",
    )
    ma_number_of_candles_for_open_order: Mapped[Optional[Decimal]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="The number of candles used to calculate the moving average (MA) for the order opening logic",
    )
    ma_number_of_candles_for_close_order: Mapped[Optional[Decimal]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="The number of candles used to calculate the moving average (MA) for the order closing logic",
    )
    copybot_v2_time_in_minutes: Mapped[Optional[Decimal]] = mapped_column(#copybot_v2_tracked_time_to_profit_copybot_v1_in_minutes
        types.Numeric,
        nullable=True,
        comment="For copy bot version 2 the time it takes for the copy bot v1 being monitored to be profitable",
    )
    use_trailing_stop: Mapped[Optional[bool]] = mapped_column(
        types.Boolean,
        nullable=True,
        comment="True, если используются трейлинг-стопы; False, если используются фиксированные стоп-лоссы."
    )
    copybot_v1_check_for_24h_profitability: Mapped[bool] = mapped_column(
        types.Boolean,
        default=False,
        comment="Checks whether bots are profitable over the last 24 hours",
    )
    copybot_v1_exclude_losing_donors: Mapped[bool] = mapped_column(
        types.Boolean,
        default=False,
        comment=(
            "Skip donors whose copybots lost money over the last 24 hours. "
            "No copy history is not a reason to skip: the filter excludes "
            "the proven-bad, it does not require the proven-good"
        ),
    )
    # Копибот v3 — третий уровень копирования: он выбирает лучшего копибота v2,
    # тот лучшего v1, а тот уже обычного бота. Той же цепочкой ходит боевой
    # `binance_bot` (`_get_best_copy_bot`), поэтому сделки такого бота — прогноз
    # результата реальной торговли.
    copybot_v3_time_in_minutes: Mapped[Optional[Decimal]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment=(
            "For copy bot version 3 the time window over which copy bots v2 "
            "are ranked. Not null marks the bot as a copybot v3"
        ),
    )
    # Ботов v3 заводят парой. Без флага баланс всегда 1000, как у всего парка,
    # и бот сравним с остальными в общих отчётах. С флагом он ведёт счёт: в
    # позицию идёт 99% баланса, количество округляется по шагу лота. Разница не
    # сводится к множителю — PnL линеен по балансу, а минимальный лот нет.
    copybot_v3_compound_balance: Mapped[bool] = mapped_column(
        types.Boolean,
        default=False,
        comment=(
            "Reinvest profit and trade 99% of the running balance with lot "
            "size rounding, mirroring the live bot. False keeps the fixed "
            "1000 balance used by the rest of the park"
        ),
    )
    # Не `is_active = false`: `active_bots_subquery` отбирает только активных,
    # и снятие флага убрало бы бота из всех отчётов — ровно там, где факт
    # остановки важнее всего.
    copybot_v3_stopped_at: Mapped[Optional[datetime]] = mapped_column(
        types.DateTime(timezone=True),
        nullable=True,
        comment=(
            "Terminal stop: the balance was restored and the bot still could "
            "not place a single trade. The bot stays active so that it keeps "
            "showing up in reports"
        ),
    )
    # Разорение больше не конец: счёт возвращается к стартовому, и бот идёт
    # заново. Само число попыток и есть результат прогноза — «слил счёт
    # четырежды за неделю» говорит о боевом боте больше, чем одна дата
    # остановки, после которой кривая просто обрывалась.
    copybot_v3_ruins: Mapped[int] = mapped_column(
        types.Integer,
        nullable=False,
        server_default="0",
        default=0,
        comment=(
            "How many times the account was wiped out and restored to the "
            "starting balance"
        ),
    )
    copybot_v3_last_ruin_at: Mapped[Optional[datetime]] = mapped_column(
        types.DateTime(timezone=True),
        nullable=True,
        comment="When the account was last wiped out and restored",
    )

    def clone(self):
        mapper = inspect(self).mapper
        columns = [c.key for c in mapper.columns]

        data = {c: getattr(self, c) for c in columns}
        return TestBot(**data)


class MarketOrder(BaseId):
    __tablename__ = "market_orders"

    client_order_id: Mapped[Optional[str]] = mapped_column(
        types.String,
        nullable=True,
        index=True,
        comment="Client-generated order ID",
    )
    exchange_name: Mapped[Optional[str]] = mapped_column(
        types.String, nullable=True, comment="Name of the exchange"
    )
    exchange_order_id: Mapped[Optional[str]] = mapped_column(
        types.String,
        nullable=True,
        index=True,
        comment="Order ID from the exchange",
    )
    symbol: Mapped[str] = mapped_column(
        types.String, nullable=True, comment="Trading symbol, e.g., BTCUSDT"
    )
    side: Mapped[str] = mapped_column(
        types.String, nullable=True, comment="Order side (BUY/SELL)"
    )
    position_side: Mapped[str] = mapped_column(
        types.String, nullable=True, comment="Position side (LONG/SHORT/BOTH)"
    )
    quote_quantity: Mapped[float] = mapped_column(
        types.Numeric, nullable=True, comment="Quantity in quote asset"
    )
    asset_quantity: Mapped[float] = mapped_column(
        types.Numeric, nullable=True, comment="Quantity in base asset"
    )
    open_order_type: Mapped[Optional[str]] = mapped_column(
        types.String,
        nullable=True,
        comment="Type of order used to open position",
    )
    close_order_type: Mapped[Optional[str]] = mapped_column(
        types.String,
        nullable=True,
        comment="Type of order used to close position",
    )
    start_price: Mapped[Optional[float]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="Price at which the order was initiated",
    )
    activation_price: Mapped[Optional[float]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="Price at which the order became active",
    )
    open_price: Mapped[Optional[float]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="Execution price for opening the position",
    )
    close_price: Mapped[Optional[float]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="Execution price for closing the position",
    )
    open_commission: Mapped[Optional[float]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="Commission paid for opening the position",
    )
    close_commission: Mapped[Optional[float]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="Commission paid for closing the position",
    )
    activation_time: Mapped[Optional[datetime]] = mapped_column(
        types.TIMESTAMP,
        nullable=True,
        index=True,
        comment="Timestamp when order became active",
    )
    open_time: Mapped[Optional[datetime]] = mapped_column(
        types.TIMESTAMP,
        nullable=True,
        index=True,
        comment="Timestamp when position was opened",
    )
    close_time: Mapped[Optional[datetime]] = mapped_column(
        types.TIMESTAMP,
        nullable=True,
        index=True,
        comment="Timestamp when position was closed",
    )
    close_reason: Mapped[Optional[str]] = mapped_column(
        types.String, nullable=True, comment="Reason for closing the position"
    )
    start_updown_ticks: Mapped[Optional[int]] = mapped_column(
        types.Integer, nullable=True, comment="Initial price movement in ticks"
    )
    trailing_stop_lose_ticks: Mapped[Optional[int]] = mapped_column(
        types.Integer, nullable=True, comment="Trailing stop loss in ticks"
    )
    trailing_stop_win_ticks: Mapped[Optional[int]] = mapped_column(
        types.Integer, nullable=True, comment="Trailing stop win in ticks"
    )
    status: Mapped[Optional[str]] = mapped_column(
        types.String, nullable=True, comment="Current status of the order"
    )
    exchange_status: Mapped[Optional[str]] = mapped_column(
        types.String,
        nullable=True,
        comment="Status of the order on the exchange",
    )
    profit_loss: Mapped[Optional[float]] = mapped_column(
        types.Numeric,
        nullable=True,
        index=True,
        comment="Profit or loss from the order",
    )


class TestOrderRollup(BigId):
    """Свёртка `test_orders` по десятиминутным блокам.

    Сырые сделки — это результат эксперимента, и удалять их «просто так»
    нельзя. Но и держать их вечно нельзя тоже: на полной скорости парк пишет
    около 490 строк в секунду, то есть 42 миллиона строк и 13 ГБ в сутки.

    Поэтому каждый закрытый блок сворачивается в одну строку на сочетание
    (бот, пара, реферальный бот), после чего сырые строки блока имеет право
    удалить ретеншн. Статистика — сколько сделок, сколько прибыльных, какой
    суммарный P/L, чем закончились — переживает чистку.

    Почему именно десять минут: все окна прибыльности копиботов кратны
    десяти (10, 20, ..., 1440, 2880), поэтому любое из них собирается из
    целых блоков без потерь.

    Причины закрытия лежат отдельными счётчиками, а не измерением ключа:
    значений всего три (`StopReasonEvent`), и так распределение сохраняется,
    не размножая строки втрое.
    """

    __tablename__ = "test_order_rollups"

    bucket_start: Mapped[datetime] = mapped_column(
        types.DateTime(timezone=True),
        nullable=False,
        comment="Начало десятиминутного блока, UTC",
    )
    bot_id: Mapped[Optional[int]] = mapped_column(
        types.Integer, nullable=True, comment="Бот, чьи это сделки"
    )
    referral_bot_id: Mapped[Optional[int]] = mapped_column(
        types.Integer,
        nullable=True,
        comment="Бот, за которым копировали, если сделки копибота",
    )
    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id"),
        nullable=False,
        comment="Стратегия парка бота",
    )
    executed_strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id"),
        nullable=False,
        comment="Стратегия, по которой исполнены сделки блока",
    )
    algorithm_version: Mapped[Optional[str]] = mapped_column(
        types.String,
        nullable=True,
        comment="Версия алгоритма; NULL у блоков, свёрнутых до её появления",
    )
    asset_symbol: Mapped[str] = mapped_column(
        types.String(255), nullable=False, comment="Пара"
    )

    orders_count: Mapped[int] = mapped_column(
        types.Integer, nullable=False, comment="Сделок в блоке"
    )
    profitable_count: Mapped[int] = mapped_column(
        types.Integer, nullable=False, comment="Из них с profit_loss > 0"
    )
    profit_loss_sum: Mapped[Optional[Decimal]] = mapped_column(
        types.Numeric, nullable=True, comment="Суммарный P/L блока"
    )
    fee_sum: Mapped[Optional[Decimal]] = mapped_column(
        types.Numeric,
        nullable=True,
        comment="Суммарная комиссия (open_fee + close_fee)",
    )

    stop_won_count: Mapped[int] = mapped_column(
        types.Integer,
        nullable=False,
        server_default="0",
        comment="Закрыто по цели (stop-won)",
    )
    stop_loosed_count: Mapped[int] = mapped_column(
        types.Integer,
        nullable=False,
        server_default="0",
        comment="Закрыто по стопу (stop-loosed)",
    )
    stop_long_lose_count: Mapped[int] = mapped_column(
        types.Integer,
        nullable=False,
        server_default="0",
        comment="Закрыто по времени удержания (stop-long-lose)",
    )

    __table_args__ = (
        # Ключ свёртки. Уникальность нужна не ради чистоты: она делает
        # пересчёт блока безопасным при любом числе повторов, в том числе
        # когда два прохода наложились друг на друга — вместо второго
        # комплекта строк получится перезапись первого.
        #
        # NULLS NOT DISTINCT (Postgres 15) — потому что referral_bot_id
        # пустой у всех некопиботов, а по умолчанию NULL не равен NULL и
        # уникальность на таких строках не работала бы вовсе.
        # Измерения стратегии входят в ключ не ради полноты: копибот
        # может сменить донора на бота другой стратегии внутри одного
        # блока. Без них две такие группы схлопнулись бы в одну строку, и
        # вторая перезаписала бы первую — часть сделок просто исчезла бы
        # из статистики.
        Index(
            "uq_test_order_rollups_key",
            "bucket_start",
            "bot_id",
            "referral_bot_id",
            "asset_symbol",
            "strategy_id",
            "executed_strategy_id",
            "algorithm_version",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("idx_test_order_rollups_bot_bucket", "bot_id", "bucket_start"),
    )
