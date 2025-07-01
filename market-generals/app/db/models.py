from typing import Optional

from datetime import datetime

from sqlalchemy import func, types, ForeignKey
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
        types.DateTime,
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="Date and time of create",
    )
    updated_at: Mapped[datetime] = mapped_column(
        types.DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Date and time of update",
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


class AssetHistory(BaseId):
    __tablename__ = "asset_history"

    id: Mapped[int] = mapped_column(
        types.Integer, primary_key=True, index=True, comment="Primary key"
    )

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
        comment="Event time (E)",
    )

    statistics_open_time: Mapped[Optional[int]] = mapped_column(
        types.BigInteger, nullable=True, comment="Statistics open time (O)"
    )

    statistics_close_time: Mapped[Optional[int]] = mapped_column(
        types.BigInteger, nullable=True, comment="Statistics close time (C)"
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


class AssetOrderBook(BaseId):
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


class TestOrder(BaseId):
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
    successful_stop_lose_ticks: Mapped[int] = mapped_column(
        types.Integer,
        nullable=False,
        default=10,
        server_default="0",
        comment="Stop-loss in ticks after trade "
        "becomes profitable (used to secure partial profit)",
    )

    start_ticks: Mapped[int] = mapped_column(
        types.Integer,
        default=0,
        server_default="0",
        nullable=False,
        comment="Start ticks",
    )

    stop_ticks: Mapped[int] = mapped_column(
        types.Integer,
        default=0,
        server_default="0",
        nullable=False,
        comment="Stop ticks",
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


class TestBot(BaseId):
    __tablename__ = "test_bots"

    symbol: Mapped[str] = mapped_column(nullable=False)
    balance: Mapped[float] = mapped_column(
        types.Numeric(precision=20, scale=10),
        nullable=False,
        comment="Balance for trading",
    )

    stop_success_ticks: Mapped[int] = mapped_column(
        types.Integer,
        nullable=False,
        default=10,
        comment="Target Profit/Close in Ticks",
    )

    stop_loss_ticks: Mapped[int] = mapped_column(
        types.Integer, nullable=False, default=5, comment="Stop-loss in ticks"
    )

    is_active: Mapped[bool] = mapped_column(
        types.Boolean, default=True, nullable=False, comment="Is active bot"
    )

    total_profit: Mapped[float] = mapped_column(
        types.Numeric(precision=20, scale=10),
        default=0,
        server_default="0",
        nullable=False,
        comment="Total profit",
    )

    successful_stop_lose_ticks: Mapped[int] = mapped_column(
        types.Integer,
        nullable=False,
        default=10,
        comment="Stop-loss in ticks after trade "
        "becomes profitable (used to secure partial profit)",
    )

    start_ticks: Mapped[int] = mapped_column(
        types.Integer,
        default=0,
        server_default="0",
        nullable=False,
        comment="Start ticks",
    )

    stop_ticks: Mapped[int] = mapped_column(
        types.Integer,
        default=0,
        server_default="0",
        nullable=False,
        comment="Stop ticks",
    )

    start_updown_ticks: Mapped[int] = mapped_column(
        types.Integer,
        default=0,
        server_default="0",
        nullable=False,
        comment="Start updown ticks",
    )
