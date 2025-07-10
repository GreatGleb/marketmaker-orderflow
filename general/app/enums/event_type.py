import enum


class StopReasonEvent(str, enum.Enum):

    STOP_LOOSED = "stop-loosed"
    STOP_WON = "stop-won"
