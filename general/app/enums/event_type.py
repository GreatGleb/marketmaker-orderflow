import enum


class StopReasonEvent(str, enum.Enum):

    STOP_LOOSED = "stop-loosed"
    STOP_WON = "stop-won"
    STOP_LONG_LOSE = "stop-long-lose"
