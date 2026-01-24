import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from polygon import RESTClient
from pytz import timezone

et_tz = timezone("America/New_York")


class RateLimiter:
    def __init__(self, max_requests: int = 5, period: int = 60):
        self.max_requests = max_requests
        self.period = period
        self.requests: list = []

    def wait_if_needed(self):
        now = time.time()
        self.requests = [req for req in self.requests if now - req < self.period]

        if len(self.requests) >= self.max_requests:
            sleep_time = self.period - (now - self.requests[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
                self.requests = []

        self.requests.append(now)


class CachedPolygonClient:
    def __init__(self, api_key: str):
        self.client = RESTClient(api_key)
        self.rate_limiter = RateLimiter(max_requests=5, period=60)

        self.qqq_cache: Dict[str, Any] = {}
        self.qqq_cache_time: Dict[str, datetime] = {}

        self.option_cache: Dict[str, Dict[str, Any]] = {}
        self.option_cache_time: Dict[str, datetime] = {}

    def _is_qqq_cache_valid(self, key: str, ttl_hours: int = 24) -> bool:
        if key not in self.qqq_cache_time:
            return False

        cache_age = datetime.now(et_tz) - self.qqq_cache_time[key]
        return cache_age < timedelta(hours=ttl_hours)

    def _is_option_cache_valid(self, key: str, ttl_minutes: int = 1) -> bool:
        if key not in self.option_cache_time:
            return False

        cache_age = datetime.now(et_tz) - self.option_cache_time[key]
        return cache_age < timedelta(minutes=ttl_minutes)

    def get_qqq_prev_close(self) -> Optional[float]:
        cache_key = "prev_close"
        if self._is_qqq_cache_valid(cache_key, ttl_hours=4):
            return self.qqq_cache.get(cache_key)

        self.rate_limiter.wait_if_needed()
        try:
            # 获取昨天的数据（动态日期）
            yesterday = (datetime.now(et_tz) - timedelta(days=1)).strftime("%Y-%m-%d")
            aggs = self.client.get_aggs("QQQ", 1, "day", yesterday, yesterday, limit=1)

            if aggs:
                self.qqq_cache[cache_key] = aggs[0].close
                self.qqq_cache_time[cache_key] = datetime.now(et_tz)
                return aggs[0].close
        except Exception as e:
            print(f"Error getting QQQ prev close: {e}")
        return None

    def get_qqq_intraday(self) -> Dict[str, Any]:
        cache_key = "intraday"
        if self._is_qqq_cache_valid(cache_key, ttl_hours=0):
            return self.qqq_cache.get(cache_key, {})

        self.rate_limiter.wait_if_needed()
        try:
            # 获取最近 2 天的数据（昨天和前天）
            # 免费版不支持获取"当天"的数据
            end_date = datetime.now(et_tz).strftime("%Y-%m-%d")
            start_date = (datetime.now(et_tz) - timedelta(days=2)).strftime("%Y-%m-%d")
            aggs = self.client.get_aggs("QQQ", 1, "day", start_date, end_date, limit=2)

            if aggs and len(aggs) >= 1:
                # 使用最新的一条数据作为"当前价格"
                last_agg = aggs[-1]
                result = {
                    "last_price": last_agg.close,
                    "intraday_high": last_agg.high,  # 使用日线的高点
                    "timestamp": last_agg.timestamp
                }
                self.qqq_cache[cache_key] = result
                self.qqq_cache_time[cache_key] = datetime.now(et_tz)
                return result
            else:
                return {"last_price": None, "intraday_high": None, "timestamp": None}
        except Exception as e:
            print(f"Error getting QQQ intraday: {e}")

        return {"last_price": None, "intraday_high": None, "timestamp": None}

    def get_qqq_historical(self, days: int = 5) -> list:
        cache_key = f"historical_{days}"
        if self._is_qqq_cache_valid(cache_key, ttl_hours=4):
            return self.qqq_cache.get(cache_key, [])

        self.rate_limiter.wait_if_needed()
        try:
            end_date = datetime.now(et_tz).strftime("%Y-%m-%d")
            start_date = (datetime.now(et_tz) - timedelta(days=days * 2)).strftime("%Y-%m-%d")

            aggs = self.client.get_aggs("QQQ", 1, "day", start_date, end_date, limit=days)

            result = []
            for agg in aggs:
                result.append({
                    "date": datetime.fromtimestamp(agg.timestamp / 1000, et_tz).date(),
                    "open": agg.open,
                    "high": agg.high,
                    "low": agg.low,
                    "close": agg.close
                })

            self.qqq_cache[cache_key] = result
            self.qqq_cache_time[cache_key] = datetime.now(et_tz)
            return result
        except Exception as e:
            print(f"Error getting QQQ historical: {e}")

        return []

    def get_option_price(self, ticker: str) -> Optional[float]:
        cache_key = ticker
        if self._is_option_cache_valid(cache_key, ttl_minutes=1):
            return self.option_cache.get(cache_key, {}).get("price")

        self.rate_limiter.wait_if_needed()
        try:
            today = datetime.now(et_tz).strftime("%Y-%m-%d")
            aggs = self.client.get_aggs(ticker, 1, "day", today, today, limit=1)

            if aggs:
                price = aggs[0].close
                self.option_cache[cache_key] = {"price": price}
                self.option_cache_time[cache_key] = datetime.now(et_tz)
                return price
        except Exception as e:
            print(f"Error getting option price for {ticker}: {e}")

        return None

    def clear_cache(self):
        self.qqq_cache.clear()
        self.qqq_cache_time.clear()
        self.option_cache.clear()
        self.option_cache_time.clear()
