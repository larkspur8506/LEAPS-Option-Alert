from typing import Dict, List, Optional, Any
from datetime import datetime, date, timedelta
from pytz import timezone
import logging
import time

from app.database.models import DailyQQQData, AlertLog, OptionPosition
from app.market.polygon_client import CachedPolygonClient
from app.market.yfinance_client import YFinanceClient

et_tz = timezone("America/New_York")

logger = logging.getLogger(__name__)


def retry_on_failure(max_retries: int = 2, delay: float = 1.0):
    """重试装饰器"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(f"[RETRY] {func.__name__} failed, attempt {attempt + 1}/{max_retries}: {e}")
                        time.sleep(delay)
            logger.error(f"[FAIL] {func.__name__} failed after {max_retries} attempts: {last_exception}")
            return None
        return wrapper
    return decorator


class DataFetcher:
    def __init__(self, polygon_client: CachedPolygonClient, db):
        self.polygon = polygon_client
        self.yfinance = YFinanceClient()
        self.db = db

    def get_qqq_data(self) -> Dict[str, Any]:
        today = datetime.now(et_tz).date()

        # Yahoo Finance 提供当日数据（解决 Polygon 免费版限制）
        yf_data = self.yfinance.get_qqq_today()

        # Polygon.io 提供历史数据（确保数据质量）
        poly_prev_close = self.polygon.get_qqq_prev_close()
        poly_historical = self.polygon.get_qqq_historical(days=5)

        # 组装数据
        result = {
            "date": today,
            "last_price": yf_data.get("last_price"),  # 来自 Yahoo Finance
            "intraday_high": yf_data.get("intraday_high"),  # 来自 Yahoo Finance
        }

        # 使用 Polygon 数据补充历史数据
        if poly_prev_close:
            result["prev_close"] = poly_prev_close
        else:
            # 如果 Polygon 失败，使用 Yahoo Finance 的前一日数据
            result["prev_close"] = self.yfinance.get_qqq_prev_close()

        if poly_historical:
            # 2日前收盘
            if len(poly_historical) >= 2:
                result["close_2_days_ago"] = poly_historical[-2].get("close")
            else:
                result["close_2_days_ago"] = yf_data.get("prev_close")

            # 3日前收盘
            if len(poly_historical) >= 3:
                result["close_3_days_ago"] = poly_historical[-3].get("close")
            else:
                result["close_3_days_ago"] = yf_data.get("prev_close")

            # 3日滚动最高
            if poly_historical:
                rolling_high = max(day.get("high", 0) for day in poly_historical[-3:])
                result["rolling_high_3d"] = rolling_high
            else:
                result["rolling_high_3d"] = self.yfinance.get_qqq_3day_high()
        else:
            # 如果 Polygon 完全失败，使用 Yahoo Finance
            result["rolling_high_3d"] = self.yfinance.get_qqq_3day_high()
            result["close_2_days_ago"] = yf_data.get("prev_close")
            result["close_3_days_ago"] = yf_data.get("prev_close")

        # 存储到数据库
        if result.get("last_price"):
            daily_data = DailyQQQData(
                date=result["date"],
                close_price=result["last_price"],
                high_price=result.get("intraday_high"),
                fetched_at=datetime.now(et_tz)
            )
            self.db.add(daily_data)

        self.db.commit()

        return result

    @retry_on_failure(max_retries=2, delay=1.0)
    def get_option_current_price(self, position) -> Optional[float]:
        """
        获取期权当前价格（多层备选 + 重试）

        获取优先级:
        1. Yahoo Finance 实时价格
        2. Polygon.io 昨日收盘价（免费版可用）

        每个数据源最多重试 2 次
        """
        polygon_ticker = self._format_polygon_ticker(position)
        yf_ticker = self._format_yahoo_finance_ticker(position)

        # 方法 1: 优先从 Yahoo Finance 获取实时价格
        try:
            price = self.yfinance.get_option_price(yf_ticker)
            if price is not None:
                logger.info(f"[OK] Yahoo Finance got option price: {yf_ticker} = ${price:.2f}")
                return price
            else:
                logger.warning(f"[WARN] Yahoo Finance returned None for: {yf_ticker}")
        except Exception as e:
            logger.error(f"[ERROR] Yahoo Finance exception for {yf_ticker}: {e}")

        logger.info(f"[INFO] Trying Polygon.io as fallback for: {yf_ticker}")

        # 方法 2: 从 Polygon.io 获取期权历史数据（免费版可用）
        try:
            historical = self.polygon.get_option_historical(polygon_ticker, days=2)
            if historical and len(historical) >= 1:
                last_day = historical[-1]
                if last_day.get("close"):
                    price = float(last_day["close"])
                    logger.info(f"[OK] Polygon.io got option price: {polygon_ticker} = ${price:.2f}")
                    return price
                else:
                    logger.warning(f"[WARN] Polygon.io no close price for: {polygon_ticker}")
            else:
                logger.warning(f"[WARN] Polygon.io no historical data for: {polygon_ticker}")
        except Exception as e:
            logger.error(f"[ERROR] Polygon.io exception for {polygon_ticker}: {e}")

        logger.error(f"[ERROR] All sources failed for option: {yf_ticker}")
        return None

    @retry_on_failure(max_retries=2, delay=1.0)
    def get_option_prev_close(self, position) -> Optional[float]:
        """
        获取期权昨日收盘价（带重试）

        使用 Polygon.io 获取期权的 End-of-Day 历史数据
        """
        polygon_ticker = self._format_polygon_ticker(position)

        try:
            historical = self.polygon.get_option_historical(polygon_ticker, days=2)
            if historical and len(historical) >= 1:
                last_day = historical[-1]
                if last_day.get("close"):
                    price = float(last_day["close"])
                    logger.info(f"[OK] Got option prev close: {polygon_ticker} = ${price:.2f}")
                    return price
            logger.warning(f"[WARN] No prev close data for: {polygon_ticker}")
        except Exception as e:
            logger.error(f"[ERROR] Failed to get option prev close {polygon_ticker}: {e}")
        return None

    def _format_yahoo_finance_ticker(self, position) -> str:
        """
        生成 Yahoo Finance 标准格式的期权代码

        格式: {Underlying}{YYMMDD}{C/P}{Strike * 1000}

        示例:
        - QQQ 2026-01-26 620 CALL -> QQQ260126C00620000
        - QQQ 2027-01-15 610 CALL -> QQQ270115C00610000
        """
        exp_date = position.expiration_date.strftime('%y%m%d')
        option_type = position.option_type[0].upper()  # C 或 P
        strike = int(position.strike_price)
        strike_str = f"{strike * 1000:08d}"  # 620 -> 00620000
        return f"{position.underlying}{exp_date}{option_type}{strike_str}"

    def _format_polygon_ticker(self, position) -> str:
        """
        生成 Polygon.io 标准格式的期权代码

        格式: O:{Underlying}{YYMMDD}{C/P}{Strike * 1000 (8位，左侧补零)}

        注意: Polygon.io 需要与 Yahoo Finance 相同的格式！

        示例:
        - QQQ 2026-01-26 620 CALL -> O:QQQ260126C00620000
        - QQQ 2027-01-15 610 CALL -> O:QQQ270115C00610000
        """
        exp_date = position.expiration_date.strftime("%y%m%d")
        option_type = "C" if position.option_type.upper() == "CALL" else "P"
        strike = int(position.strike_price)
        strike_str = f"{strike * 1000:08d}"

        ticker = f"O:{position.underlying}{exp_date}{option_type}{strike_str}"
        return ticker

    def calculate_dte(self, expiration_date: date) -> int:
        today = datetime.now(et_tz).date()
        dte = (expiration_date - today).days
        return max(0, dte)

    def clear_cache(self):
        self.polygon.clear_cache()
