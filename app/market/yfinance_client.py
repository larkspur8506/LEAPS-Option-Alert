import yfinance as yf
from datetime import datetime, date, timedelta
from pytz import timezone
import time
from time import sleep
from typing import Optional

et_tz = timezone("America/New_York")


class YFinanceClient:
    def __init__(self):
        self.last_request_time = 0
        self.min_request_interval = 2  # 最小请求间隔 2 秒，避免限流

    def _wait_for_rate_limit(self):
        """避免触发限流"""
        now = time.time()
        elapsed = now - self.last_request_time

        if elapsed < self.min_request_interval:
            sleep(self.min_request_interval - elapsed)

        self.last_request_time = now

    def get_qqq_today(self) -> dict:
        """获取 QQQ 当日数据（只获取当日，避免限流）"""
        self._wait_for_rate_limit()

        try:
            ticker = yf.Ticker("QQQ")

            # 只获取当天的数据（1 分钟间隔）
            data = ticker.history(period="1d", interval="1m")

            if data is not None and not data.empty:
                latest = data.iloc[-1]

                result = {
                    "last_price": float(latest["Close"]),
                    "intraday_high": float(data["High"].max()),
                    "timestamp": latest.name
                }
                return result
            else:
                return {"last_price": None, "intraday_high": None, "timestamp": None}
        except Exception as e:
            print(f"Error getting QQQ today: {e}")
            return {"last_price": None, "intraday_high": None, "timestamp": None}

    def get_qqq_prev_close(self) -> Optional[float]:
        """获取昨日收盘价（使用历史数据，避免限流）"""
        self._wait_for_rate_limit()

        try:
            ticker = yf.Ticker("QQQ")

            # 获取过去 5 天的数据
            data = ticker.history(period="5d")

            if data is not None and len(data) >= 2:
                # 返回倒数第 2 天的收盘价（昨天）
                prev_close = float(data.iloc[-2]["Close"])
                return prev_close
            else:
                return None
        except Exception as e:
            print(f"Error getting QQQ prev close: {e}")
            return None

    def get_qqq_3day_high(self) -> Optional[float]:
        """获取 3 日滚动最高（避免限流）"""
        self._wait_for_rate_limit()

        try:
            ticker = yf.Ticker("QQQ")

            # 获取过去 5 天的数据（确保有 3 个交易日）
            data = ticker.history(period="5d")

            if data is not None and len(data) >= 3:
                # 计算过去 3 个交易日的最高价
                high = float(data["High"].iloc[-3:].max())
                return high
            else:
                return None
        except Exception as e:
            print(f"Error getting QQQ 3day high: {e}")
            return None

    def get_option_price(self, ticker: str) -> Optional[float]:
        """获取期权价格（避免限流）"""
        self._wait_for_rate_limit()

        try:
            # 尝试多种格式
            ticker_obj = yf.Ticker(ticker)

            # 方法 1: 直接获取
            try:
                data = ticker_obj.history(period="5d", interval="1d")

                if data is not None and not data.empty:
                    latest = data.iloc[-1]
                    return float(latest["Close"])
            except Exception:
                pass

            return None
        except Exception as e:
            print(f"Error getting option price for {ticker}: {e}")
            return None
