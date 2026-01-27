from typing import Dict, List, Optional, Any
from datetime import datetime, date, timedelta
from pytz import timezone
import logging
import time
import pandas as pd
import yfinance as yf

from app.database.models import DailyQQQData, AlertLog, OptionPosition
from sqlalchemy.exc import IntegrityError
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
        """
        获取 QQQ 价格及技术指标
        
        策略优先级:
        Level 1 (yfinance): 获取完整历史数据计算。
        Level 2 (Polygon 灾备): 获取历史聚合数据重构 DataFrame，并打入实时最新价。
        """
        df = None
        
        # ---------------------------------------------------------
        # Level 1: YFinance (Primary)
        # ---------------------------------------------------------
        try:
            ticker = yf.Ticker("QQQ")
            # 获取 1 年数据，确保有足够的历史计算 MA200
            df = ticker.history(period="1y")
            
            if df is not None and not df.empty:
                logger.info("[INFO] Successfully fetched QQQ history from yfinance")
            else:
                logger.warning("[WARN] yfinance returned empty QQQ history")
                df = None
        except Exception as e:
            logger.error(f"[ERROR] yfinance QQQ history failed: {e}")
            df = None

        # ---------------------------------------------------------
        # Level 2: Polygon Fallback (Reconstruct DataFrame)
        # ---------------------------------------------------------
        if df is None:
            logger.info("[FALLBACK] Attempting Polygon historical reconstruction...")
            try:
                # 获取约 300 天数据 (覆盖 1 年交易日)
                poly_data = self.polygon.get_qqq_historical(days=300)
                
                if poly_data:
                    # 1. 转换为 DataFrame
                    df = pd.DataFrame(poly_data)
                    
                    # 2. 映射列名和索引
                    # Polygon: date, open, high, low, close
                    # Pandas/yfinance: Date(Index), Open, High, Low, Close
                    df['Date'] = pd.to_datetime(df['date'])
                    df.set_index('Date', inplace=True)
                    df.rename(columns={
                        'open': 'Open', 
                        'high': 'High', 
                        'low': 'Low', 
                        'close': 'Close'
                    }, inplace=True)
                    
                    # 删除多余列
                    if 'date' in df.columns:
                        del df['date']
                        
                    # 3. 实时补丁 (Realtime Patch)
                    # Polygon 免费版通常延迟或只给到昨日，需手动获取今日最新价拼接到 DataFrame
                    try:
                        yf_today = self.yfinance.get_qqq_today()
                        last_price = yf_today.get("last_price")
                        
                        if last_price:
                            today_date = pd.Timestamp(datetime.now(et_tz).date())
                            intraday_high = yf_today.get("intraday_high", last_price)
                            
                            # 构建今日行
                            new_row = pd.Series({
                                'Open': last_price, # 近似值
                                'High': intraday_high,
                                'Low': last_price, # 近似值
                                'Close': last_price,
                                'Volume': 0 # 占位
                            }, name=today_date)
                            
                            # 追加或更新
                            if today_date in df.index:
                                df.loc[today_date] = new_row
                            else:
                                df = pd.concat([df, pd.DataFrame([new_row])])
                                
                            logger.info(f"[PATCH] Appended realtime price ${last_price} to Polygon history")
                            
                    except Exception as e:
                         logger.warning(f"[WARN] Failed to patch realtime price: {e}")
                
            except Exception as e:
                logger.error(f"[ERROR] Polygon fallback failed: {e}")

        # ---------------------------------------------------------
        # Process DataFrame
        # ---------------------------------------------------------
        if df is not None and not df.empty:
            return self._process_qqq_df(df)
            
        return {}

    def _process_qqq_df(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        统一处理 Pandas DataFrame 计算指标
        无论数据源是 yfinance 还是 Polygon，经过规整后都在这里统一通过。
        """
        try:
            # 确保按日期排序
            df = df.sort_index()
            close_prices = df["Close"]
            
            # 判断是否降级 (不足以计算 MA200)
            is_degraded = len(df) < 200

            # MA20 / MA200
            df["ma20"] = close_prices.rolling(window=20).mean()
            df["ma200"] = close_prices.rolling(window=200).mean()

            # Bollinger Bands (20, 2)
            std_20 = close_prices.rolling(window=20).std()
            df["bb_upper"] = df["ma20"] + 2 * std_20
            df["bb_lower"] = df["ma20"] - 2 * std_20

            # RSI (14) - Wilder's Smoothing (com=13)
            delta = close_prices.diff()
            gain = (delta.where(delta > 0, 0)).fillna(0)
            loss = (-delta.where(delta < 0, 0)).fillna(0)
            avg_gain = gain.ewm(com=13, adjust=False).mean()
            avg_loss = loss.ewm(com=13, adjust=False).mean()
            rs = avg_gain / avg_loss
            df["rsi"] = 100 - (100 / (1 + rs))

            # 提取最后一行
            latest = df.iloc[-1]
            last_price = float(latest["Close"])
            intraday_high = float(latest["High"])

            # 获取昨日 (-2) 和 3日前 (-4)
            # 必须防御性处理越界，特别是在数据刚开始建立时
            total_len = len(df)
            
            if total_len >= 2:
                prev_close = float(df["Close"].iloc[-2])
            else:
                prev_close = last_price
                
            if total_len >= 4:
                three_day_prev_close = float(df["Close"].iloc[-4])
            else:
                three_day_prev_close = float(df["Close"].iloc[0])

            # 组装结果
            result = {
                "date": datetime.now(et_tz).date(),
                "last_price": last_price,
                "intraday_high": intraday_high,
                
                # 指标 (Pandas series 取最后一行可能为 NaN，需处理)
                "ma20": float(latest["ma20"]) if pd.notna(latest["ma20"]) else None,
                "ma200": float(latest["ma200"]) if pd.notna(latest["ma200"]) else None,
                "rsi": float(latest["rsi"]) if pd.notna(latest["rsi"]) else None,
                "bb_upper": float(latest["bb_upper"]) if pd.notna(latest["bb_upper"]) else None,
                "bb_lower": float(latest["bb_lower"]) if pd.notna(latest["bb_lower"]) else None,
                
                # 历史参考
                "prev_close": prev_close,
                "three_day_prev_close": three_day_prev_close,
                
                # 状态位
                "is_degraded": is_degraded
            }

            # 存入数据库
            self._save_daily_data(result)

            return result
        except Exception as e:
            logger.error(f"[ERROR] processing QQQ DF: {e}")
            return {}

    def _save_daily_data(self, result: Dict[str, Any]):
        try:
            if not result.get("last_price"):
                return

            date_val = result["date"]
            # Check existing
            existing = self.db.query(DailyQQQData).filter_by(date=date_val).first()
            if existing:
                existing.close_price = result["last_price"]
                existing.high_price = result.get("intraday_high")
                existing.fetched_at = datetime.now(et_tz)
            else:
                daily = DailyQQQData(
                    date=date_val,
                    close_price=result["last_price"],
                    high_price=result.get("intraday_high"),
                    fetched_at=datetime.now(et_tz)
                )
                self.db.add(daily)
            
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            logger.error(f"Error saving daily data: {e}")

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

    @retry_on_failure(max_retries=2, delay=1.0)
    def get_vix_index(self) -> Optional[float]:
        """
        获取 VIX 波动率指数的最新价格
        
        Returns:
            Optional[float]: VIX 指数值，获取失败时返回 None
        """
        try:
            vix_ticker = yf.Ticker("^VIX")
            vix_data = vix_ticker.history(period="1d")
            
            if vix_data is not None and not vix_data.empty:
                vix_value = float(vix_data["Close"].iloc[-1])
                logger.info(f"[OK] Successfully fetched VIX index: {vix_value:.2f}")
                return vix_value
            else:
                logger.warning("[WARN] yfinance returned empty VIX data")
                return None
                
        except Exception as e:
            logger.error(f"[ERROR] Failed to fetch VIX index: {e}")
            return None
