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
        types.DateTime, nullable=False, comment="Event time (E)"
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
