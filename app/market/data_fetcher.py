from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional
from pytz import timezone

from .polygon_client import CachedPolygonClient
from .yfinance_client import YFinanceClient
from app.config import Config

et_tz = timezone("America/New_York")


class DataFetcher:
    def __init__(self, polygon_client: CachedPolygonClient, db):
        self.polygon = polygon_client
        self.yfinance = YFinanceClient()
        self.db = db

    def get_qqq_data(self) -> Dict[str, Any]:
        """获取 QQ Q 数据（当日数据用 yfinance，历史数据用 Polygon）"""
        today = datetime.now(et_tz).date()

        # 使用 yfinance 获取当日数据
        yf_data = self.yfinance.get_qqq_today()

        # 使用 Polygon.io 获取历史数据
        prev_close = self.polygon.get_qqq_prev_close()
        historical = self.polygon.get_qqq_historical(days=5)

        # 组装数据
        result = {
            "date": today,
            "last_price": yf_data.get("last_price"),
            "prev_close": prev_close,
            "intraday_high": yf_data.get("intraday_high"),
            "rolling_high_3d": None,
            "close_2_days_ago": None,
            "close_3_days_ago": None
        }

        if len(historical) >= 2:
            result["close_2_days_ago"] = historical[-2].get("close")

        if len(historical) >= 3:
            result["close_3_days_ago"] = historical[-3].get("close")

        if len(historical) >= 3:
            rolling_high = max(
                historical[-3].get("high", 0),
                historical[-2].get("high", 0),
                historical[-1].get("high", 0)
            )
            result["rolling_high_3d"] = rolling_high

        self._save_daily_qqq_data(result)

        return result

    def _save_daily_qqq_data(self, data: Dict[str, Any]):
        from app.database.models import DailyQQQData
        from sqlalchemy import and_

        existing = self.db.query(DailyQQQData).filter(
            DailyQQQData.date == data["date"]
        ).first()

        if existing:
            existing.close_price = data["last_price"]
            existing.high_price = data.get("intraday_high")
            existing.fetched_at = datetime.now(et_tz)
        else:
            daily_data = DailyQQQData(
                date=data["date"],
                close_price=data["last_price"],
                high_price=data.get("intraday_high"),
                fetched_at=datetime.now(et_tz)
            )
            self.db.add(daily_data)

        self.db.commit()

    def get_option_current_price(self, position) -> Optional[float]:
        option_ticker = self._format_option_ticker(position)
        
        # 优先使用 Polygon.io 获取期权价格
        price = self.polygon.get_option_price(option_ticker)
        if price is not None:
            return price
        
        # 如果 Polygon 失败，使用 Yahoo Finance 作为备用
        try:
            yf_option_ticker = f"{position.underlying}{position.expiration_date.strftime('%y%m%d')}{position.option_type[0]}{int(position.strike_price)}"
            price = self.yfinance.get_option_price(yf_option_ticker)
            return price
        except Exception:
            return None

    def _format_option_ticker(self, position) -> str:
        exp_date = position.expiration_date.strftime("%y%m%d")
        option_type = "C" if position.option_type == "CALL" else "P"
        strike = int(position.strike_price)

        ticker = f"O:{position.underlying}{exp_date}{option_type}{strike}"
        return ticker

    def calculate_dte(self, expiration_date: date) -> int:
        today = datetime.now(et_tz).date()
        dte = (expiration_date - today).days
        return max(0, dte)

    def clear_cache(self):
        self.polygon.clear_cache()
