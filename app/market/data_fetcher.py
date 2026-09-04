from typing import Dict, Any
from datetime import datetime
from pytz import timezone
import logging
import time
import pandas as pd
import yfinance as yf

et_tz = timezone("America/New_York")

logger = logging.getLogger(__name__)


class DataFetcher:
    def __init__(self):
        # 缓存机制，避免频繁请求 yfinance 导致被封禁
        self._ndx_cache = None
        self._ndx_cache_time = 0.0

    def get_ndx_data(self) -> Dict[str, Any]:
        """
        获取 Nasdaq-100 (^NDX) 价格及技术指标。

        获取至少 2 年历史数据 (period="2y")，以安全支撑 SMA200 及倒数第 253 个交易日 (约一年前) 的价格计算。
        """
        from app.scheduler.trading_hours import is_market_open_now

        current_time = time.time()

        # 智能防封禁缓存逻辑
        if self._ndx_cache:
            if not is_market_open_now():
                logger.debug("[CACHE] Market is closed, using permanent cached NDX data")
                return self._ndx_cache
            elif current_time - self._ndx_cache_time < 60:
                logger.debug("[CACHE] Market is open, using 60s cached NDX data")
                return self._ndx_cache

        df = None

        # Level 1: YFinance (Primary) - period="2y"
        try:
            ticker = yf.Ticker("^NDX")
            df = ticker.history(period="2y")

            if df is not None and not df.empty:
                logger.info(f"[INFO] Successfully fetched ^NDX history from yfinance ({len(df)} rows)")
            else:
                logger.warning("[WARN] yfinance returned empty ^NDX history")
                df = None
        except Exception as e:
            logger.error(f"[ERROR] yfinance ^NDX history failed: {e}")
            df = None

        # Level 2: Fallback (Graceful fallback)
        if df is None:
            logger.info("[FALLBACK] Attempting fallback for ^NDX...")
            pass

        if df is not None and not df.empty:
            result = self._process_ndx_df(df)
            if result:
                self._ndx_cache = result
                self._ndx_cache_time = current_time
            return result

        return {}

    def _process_ndx_df(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        处理 ^NDX DataFrame 并计算相关指标。
        """
        return self.calculate_technical_indicators(df, ticker="^NDX")

    @staticmethod
    def calculate_technical_indicators(df: pd.DataFrame, ticker: str = "^NDX") -> Dict[str, Any]:
        """
        纯指标计算函数。

        参数:
            df: 包含 Close, High, Low, Volume 的日K线 DataFrame
            ticker: 标的代码，默认 ^NDX

        指标定义与要求:
            - SMA200: 200交易日收盘移动平均
            - RSI14: Wilder's Smoothing RSI (com=13)
            - is_above_sma200_3d: 最近连续3个交易日收盘价是否均高于当日SMA200
            - price_1y_ago: 约252个交易日前的收盘价 (iloc[-253])，数据不足253天时显式标记不可用 (None)
            - data_timestamp: 最新数据所在时间
            - is_data_valid: 数据是否达到最小可用标准
        """
        if df is None or df.empty:
            return {
                "ticker": ticker,
                "is_data_valid": False,
                "last_price": None,
                "rsi": None,
                "ma200": None,
                "is_above_sma200_3d": False,
                "price_1y_ago": None,
                "price_1y_ago_available": False,
                "data_timestamp": None
            }

        try:
            # 基础数据清洗: 确保按索引排序、去除重复索引、丢弃 Close 为空的数据行
            clean_df = df.sort_index().copy()
            clean_df = clean_df[~clean_df.index.duplicated(keep="last")]
            clean_df = clean_df.dropna(subset=["Close"])
            total_len = len(clean_df)

            if total_len == 0:
                return {"ticker": ticker, "is_data_valid": False}

            close_prices = clean_df["Close"]

            # 1. 均线计算
            clean_df["ma20"] = close_prices.rolling(window=20).mean()
            clean_df["ma200"] = close_prices.rolling(window=200).mean()

            # 2. 布林带 (20, 2)
            std_20 = close_prices.rolling(window=20).std()
            clean_df["bb_upper"] = clean_df["ma20"] + 2 * std_20
            clean_df["bb_lower"] = clean_df["ma20"] - 2 * std_20

            # 3. RSI (14) - Wilder's Smoothing
            delta = close_prices.diff()
            gain = (delta.where(delta > 0, 0)).fillna(0)
            loss = (-delta.where(delta < 0, 0)).fillna(0)
            avg_gain = gain.ewm(com=13, adjust=False).mean()
            avg_loss = loss.ewm(com=13, adjust=False).mean()
            rs = avg_gain / avg_loss
            clean_df["rsi"] = 100 - (100 / (1 + rs))

            latest = clean_df.iloc[-1]
            last_price = float(latest["Close"])
            intraday_high = float(latest["High"]) if "High" in clean_df.columns else last_price

            prev_close = float(clean_df["Close"].iloc[-2]) if total_len >= 2 else last_price

            # 4. 连续 3 个交易日收盘价大于 SMA200 (按最近有效交易日)
            is_above_sma200_3d = False
            is_below_sma200_3d = False
            consec_above = 0
            consec_below = 0

            if total_len >= 3 and pd.notna(clean_df["ma200"].iloc[-3:]).all():
                is_above_sma200_3d = bool((clean_df["Close"].iloc[-3:] > clean_df["ma200"].iloc[-3:]).all())
                is_below_sma200_3d = bool((clean_df["Close"].iloc[-3:] < clean_df["ma200"].iloc[-3:]).all())

            if total_len > 0 and pd.notna(clean_df["ma200"].iloc[-1]):
                for i in range(1, min(total_len, 200)):
                    close_p = float(clean_df["Close"].iloc[-i])
                    ma_p = float(clean_df["ma200"].iloc[-i])
                    if pd.notna(ma_p):
                        if close_p > ma_p and consec_below == 0:
                            consec_above += 1
                        elif close_p < ma_p and consec_above == 0:
                            consec_below += 1
                        else:
                            break

            # 5. 约一年前收盘价 (精确取倒数第 253 个有效交易日，即 iloc[-253])
            # 如果数据长度 >= 253，则取 -253 处的收盘价；若不足，则明确返回不可用状态
            price_1y_ago = None
            price_1y_ago_available = False
            if total_len >= 253:
                price_1y_ago = float(clean_df["Close"].iloc[-253])
                price_1y_ago_available = True

            # 6. 数据新鲜度与时间戳
            data_timestamp = str(clean_df.index[-1]) if len(clean_df.index) > 0 else None

            result = {
                "ticker": ticker,
                "date": datetime.now(et_tz).date(),
                "data_timestamp": data_timestamp,
                "is_data_valid": total_len >= 200 and pd.notna(latest["ma200"]),
                "last_price": last_price,
                "intraday_high": intraday_high,
                "prev_close": prev_close,

                # 指标
                "ma20": float(latest["ma20"]) if pd.notna(latest["ma20"]) else None,
                "ma200": float(latest["ma200"]) if pd.notna(latest["ma200"]) else None,
                "rsi": float(latest["rsi"]) if pd.notna(latest["rsi"]) else None,
                "bb_upper": float(latest["bb_upper"]) if pd.notna(latest["bb_upper"]) else None,
                "bb_lower": float(latest["bb_lower"]) if pd.notna(latest["bb_lower"]) else None,

                # 规则所需核心判定字段
                "is_above_sma200_3d": is_above_sma200_3d,
                "is_below_sma200_3d": is_below_sma200_3d,
                "consec_above": consec_above,
                "consec_below": consec_below,
                "price_1y_ago": price_1y_ago,
                "price_1y_ago_available": price_1y_ago_available,
                "total_records": total_len
            }

            return result
        except Exception as e:
            logger.error(f"[ERROR] calculating indicators for {ticker}: {e}")
            return {
                "ticker": ticker,
                "is_data_valid": False,
                "error": str(e)
            }
