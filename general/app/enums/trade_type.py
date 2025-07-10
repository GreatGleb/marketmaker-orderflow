import enum


class TradeType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
