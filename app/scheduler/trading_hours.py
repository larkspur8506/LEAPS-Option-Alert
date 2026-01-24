from datetime import datetime, date
from typing import Optional
from pytz import timezone
from pandas_market_calendars import get_calendar

et_tz = timezone("America/New_York")
nyse_calendar = get_calendar("XNYS")


def is_trading_day(dt: Optional[datetime] = None) -> bool:
    if dt is None:
        dt = datetime.now(et_tz)

    date_to_check = dt.date()
    schedule = nyse_calendar.schedule(start_date=date_to_check, end_date=date_to_check)

    if schedule.empty:
        return False

    market_open = schedule.iloc[0]["market_open"]
    market_close = schedule.iloc[0]["market_close"]

    return market_open is not None and market_close is not None


def is_trading_time(dt: Optional[datetime] = None) -> bool:
    if dt is None:
        dt = datetime.now(et_tz)

    if not is_trading_day(dt):
        return False

    dt_et = dt.astimezone(et_tz)

    schedule = nyse_calendar.schedule(start_date=dt_et.date(), end_date=dt_et.date())

    if schedule.empty:
        return False

    market_open = schedule.iloc[0]["market_open"].astimezone(et_tz)
    market_close = schedule.iloc[0]["market_close"].astimezone(et_tz)

    return market_open <= dt_et <= market_close


def get_market_open_time(dt: Optional[datetime] = None) -> datetime:
    if dt is None:
        dt = datetime.now(et_tz)

    schedule = nyse_calendar.schedule(start_date=dt.date(), end_date=dt.date())

    if not schedule.empty:
        market_open = schedule.iloc[0]["market_open"].astimezone(et_tz)
        return market_open

    return dt.replace(hour=9, minute=30, second=0, microsecond=0)


def get_market_close_time(dt: Optional[datetime] = None) -> datetime:
    if dt is None:
        dt = datetime.now(et_tz)

    schedule = nyse_calendar.schedule(start_date=dt.date(), end_date=dt.date())

    if not schedule.empty:
        market_close = schedule.iloc[0]["market_close"].astimezone(et_tz)
        return market_close

    return dt.replace(hour=16, minute=0, second=0, microsecond=0)


def is_market_open_now() -> bool:
    return is_trading_time()


def get_current_time_et() -> datetime:
    return datetime.now(et_tz)
