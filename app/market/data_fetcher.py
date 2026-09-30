from typing import Dict, Any
from datetime import datetime
from pytz import timezone
import logging
import time
import pandas as pd
import yfinance as yf

et_tz = timezone("America/New_York")

logger = logging.getLogger(__name__)


def _closed_bar_indicators(clean_df: pd.DataFrame, live_bar_is_provisional: bool) -> Dict[str, Any]:
    """
    计算"最后一根已收盘 bar"的等价指标 (收盘确认制)。

    - live_bar_is_provisional=True 表示最后一根 bar 是盘中未收盘的实时 bar,
      此时参考位置回退一根 (倒数第二根), 即"上一交易日收盘"。
    - 参考位置不足 253 根时, closed_price_1y_ago 为 None (不虚构)。
    - 任何异常都不抛出, 只返回不可用标记, 不影响既有字段。
    """
    unavailable: Dict[str, Any] = {
        "closed_bar_date": None,
        "closed_price": None,
        "closed_rsi": None,
        "closed_ma200": None,
        "closed_is_above_sma200_3d": False,
        "closed_price_1y_ago": None,
        "closed_indicators_available": False,
    }
    try:
        total = len(clean_df)
        if total == 0:
            return unavailable

        pos = total - 2 if (live_bar_is_provisional and total >= 2) else total - 1
        row = clean_df.iloc[pos]

        result = dict(unavailable)
        result["closed_bar_date"] = str(clean_df.index[pos])
        if pd.notna(row["Close"]):
            result["closed_price"] = float(row["Close"])
        if "rsi" in clean_df.columns and pd.notna(row["rsi"]):
            result["closed_rsi"] = float(row["rsi"])
        if "ma200" in clean_df.columns and pd.notna(row["ma200"]):
            result["closed_ma200"] = float(row["ma200"])

        if pos >= 2 and pd.notna(clean_df["ma200"].iloc[pos - 2:pos + 1]).all():
            result["closed_is_above_sma200_3d"] = bool(
                (clean_df["Close"].iloc[pos - 2:pos + 1] > clean_df["ma200"].iloc[pos - 2:pos + 1]).all()
            )

        i1y = pos - 252
        if i1y >= 0:
            result["closed_price_1y_ago"] = float(clean_df["Close"].iloc[i1y])

        result["closed_indicators_available"] = (
            result["closed_price"] is not None and result["closed_rsi"] is not None
        )
        return result
    except Exception as exc:
        logger.warning(f"[WARN] closed-bar indicator computation failed: {exc}")
        return unavailable


class DataFetcher:
    def __init__(self):
        # 缓存机制，避免频繁请求 yfinance 导致被封禁
        self._qqq_cache = None
        self._qqq_cache_time = 0.0
        # 缓存对应的"最近交易日": 交易日切换后缓存立即失效, 避免长跑进程
        # 在收盘后永久返回上一交易日的缓存数据
        self._qqq_cache_trading_day = None

    def _cache_still_current(self) -> bool:
        """缓存是否属于当前最近交易日 (跨交易日/长假后自动失效)。"""
        if not self._qqq_cache:
            return False
        try:
            from app.scheduler.trading_hours import get_latest_trading_day
            latest = get_latest_trading_day()
        except Exception:
            return True  # 日历不可用时不做额外约束, 退回原有 60s/收盘缓存语义
        return latest is not None and latest == self._qqq_cache_trading_day

    def get_qqq_data(self) -> Dict[str, Any]:
        """
        获取 QQQ (纳指100ETF) 价格及技术指标。

        获取至少 2 年历史数据 (period="2y")，以安全支撑 SMA200 及倒数第 253 个交易日 (约一年前) 的价格计算。
        """
        from app.scheduler.trading_hours import is_market_open_now, get_latest_trading_day

        current_time = time.time()

        # 智能防封禁缓存逻辑 (按交易日失效)
        if self._cache_still_current():
            if not is_market_open_now():
                logger.debug("[CACHE] Market is closed, using cached QQQ data for the same trading day")
                return self._qqq_cache
            elif current_time - self._qqq_cache_time < 60:
                logger.debug("[CACHE] Market is open, using 60s cached QQQ data")
                return self._qqq_cache

        df = None

        # Level 1: YFinance (Primary) - period="2y"
        try:
            ticker = yf.Ticker("QQQ")
            df = ticker.history(period="2y")

            if df is not None and not df.empty:
                logger.info(f"[INFO] Successfully fetched QQQ history from yfinance ({len(df)} rows)")
            else:
                logger.warning("[WARN] yfinance returned empty QQQ history")
                df = None
        except Exception as e:
            logger.error(f"[ERROR] yfinance QQQ history failed: {e}")
            df = None

        if df is not None and not df.empty:
            result = self._process_qqq_df(df)
            if result:
                self._qqq_cache = result
                self._qqq_cache_time = current_time
                try:
                    self._qqq_cache_trading_day = get_latest_trading_day()
                except Exception:
                    self._qqq_cache_trading_day = None
            return result

        return {}

    def get_market_breadth(self) -> Dict[str, Any]:
        """
        获取辅助市场宽度指标 (S&P 500 / VIX), 仅用于每日简报展示。

        定位: 辅助指标。任何一项获取失败都不抛出异常、不阻塞 LEAPS 主流程,
        缺失项以 None 表示 (日报渲染为 N/A)。
        """
        result: Dict[str, Any] = {"sp500": None, "vix": None}

        for key, symbol in (("sp500", "^GSPC"), ("vix", "^VIX")):
            try:
                df = yf.Ticker(symbol).history(period="5d")
                if df is None or df.empty or "Close" not in df.columns:
                    result[key] = None
                    continue

                close = df["Close"].dropna()
                if len(close) == 0:
                    result[key] = None
                    continue

                price = float(close.iloc[-1])
                prev = float(close.iloc[-2]) if len(close) >= 2 else None
                change = ((price - prev) / prev * 100.0) if prev else None

                result[key] = {"price": price, "prev_close": prev, "change_pct": change}
            except Exception as e:
                logger.warning(f"[WARN] market breadth fetch failed for {symbol}: {e}")
                result[key] = None

        return result

    def _process_qqq_df(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        处理 QQQ DataFrame 并计算相关指标。

        盘中时段最后一根日K是"未收盘"的实时 bar, 因此显式标记
        `live_bar_is_provisional=True`, 由指标层额外算出一套**已收盘 bar**
        的指标 (`closed_*`), 供开仓信号判定使用 (收盘确认制)。
        """
        from app.scheduler.trading_hours import is_market_open_now

        try:
            provisional = bool(is_market_open_now())
        except Exception:
            provisional = False

        return self.calculate_technical_indicators(
            df, ticker="QQQ", live_bar_is_provisional=provisional
        )

    @staticmethod
    def calculate_technical_indicators(df: pd.DataFrame, ticker: str = "QQQ",
                                       live_bar_is_provisional: bool = False) -> Dict[str, Any]:
        """
        纯指标计算函数。

        参数:
            df: 包含 Close, High, Low, Volume 的日K线 DataFrame
            ticker: 标的代码，默认 QQQ
            live_bar_is_provisional: 最后一根 bar 是否为"盘中未收盘"的实时 bar。
                True 时会额外输出一组 `closed_*` 指标 (基于最后一根**已收盘** bar),
                供开仓信号判定使用, 避免盘中噪声制造假信号。
                默认 False, 即 `last_price`/`rsi` 等既有字段语义完全不变。

        指标定义与要求:
            - SMA200: 200交易日收盘移动平均
            - RSI14: Wilder's Smoothing RSI (com=13)
            - is_above_sma200_3d: 最近连续3个交易日收盘价是否均高于当日SMA200
            - price_1y_ago: 约252个交易日前的收盘价 (iloc[-253])，数据不足253天时显式标记不可用 (None)
            - data_timestamp: 最新数据所在时间
            - is_data_valid: 数据是否达到最小可用标准
            - closed_*: 最后一根已收盘 bar 的等价指标 (收盘确认制, 见上)
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
                "data_timestamp": None,
                # 收盘确认制字段 (无数据时全部不可用)
                "live_bar_is_provisional": bool(live_bar_is_provisional),
                "closed_bar_date": None,
                "closed_price": None,
                "closed_rsi": None,
                "closed_ma200": None,
                "closed_is_above_sma200_3d": False,
                "closed_price_1y_ago": None,
                "closed_indicators_available": False,
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

            # 7. 收盘确认制: 最后一根已收盘 bar 的等价指标
            #    盘中 (live_bar_is_provisional=True) 时, 最后一根 bar 未收盘,
            #    开仓判定改用倒数第二根 bar, 避免盘中 RSI 瞬时跌破阈值制造假信号。
            closed = _closed_bar_indicators(clean_df, live_bar_is_provisional)

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
                "total_records": total_len,

                # 收盘确认制字段 (开仓信号判定使用)
                "live_bar_is_provisional": bool(live_bar_is_provisional),
                "closed_bar_date": closed["closed_bar_date"],
                "closed_price": closed["closed_price"],
                "closed_rsi": closed["closed_rsi"],
                "closed_ma200": closed["closed_ma200"],
                "closed_is_above_sma200_3d": closed["closed_is_above_sma200_3d"],
                "closed_price_1y_ago": closed["closed_price_1y_ago"],
                "closed_indicators_available": closed["closed_indicators_available"],
            }

            return result
        except Exception as e:
            logger.error(f"[ERROR] calculating indicators for {ticker}: {e}")
            return {
                "ticker": ticker,
                "is_data_valid": False,
                "error": str(e)
            }
