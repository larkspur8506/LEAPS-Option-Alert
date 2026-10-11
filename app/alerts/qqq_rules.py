"""QQQ LEAPS 策略规则引擎 (纯函数, 无 DB 依赖)。

入场 (2026-10-11 复测修订):
  1. RSI14 < 35 (收盘确认制: 用最后一根已收盘 bar 判定)
  2. 收盘价 > 一年前收盘价 (52 周趋势过滤)
     —— 这是承重墙: 去掉它 2001-2026 样本最差单笔 -95.8%、复利权益 0.02x (爆仓)。
     原「连续 3 日 > SMA200」条件已于 2026-10-11 移除: 与 52 周过滤高度重叠,
     去掉后 CAGR 12.6% -> 15.1%、最差单笔不变 (-39.2%)、两段样本都更好。

持仓退出 (先到先出):
  - RSI14 > TP 阈值 (默认 65): 止盈全部清仓
  - 持仓超过 N 个交易日仍未回本 (默认关闭; 复测表明时间止损制造了历史唯二亏损)
  - 距到期 < N 天 (默认 90): DTE 强制平仓 (2026-10 复测: 1y 合约必须收紧到 90, 否则慢修复交易被卡在 theta 加速区)
  - 总盈利 ≥ half_tp 比例 (默认 +50%, 0=关闭): 提醒卖出一半锁定利润 (HALF_TP, 仅提醒)

加仓: 相对 signal_base_price 回撤达档位 (-15%/-25%) 且 RSI 仍 < 入场阈值,
      最多加到 max_quantity 张。回测: 加仓将 p10 从 -21% 收窄到 +24%。
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 入场信号
# ---------------------------------------------------------------------------

def check_entry_signal(qqq_data: Dict[str, Any], rsi_threshold: float = 35.0) -> Optional[Dict[str, Any]]:
    """
    判定 QQQ LEAPS 入场信号 (收盘确认制)。

    返回 None (未触发/数据不足) 或:
    {
        "signal_base_price": float,   # 已收盘 bar 收盘价 (加仓回撤基准)
        "signal_rsi": float,
        "signal_bar_date": str,       # 判定基准 bar (YYYY-MM-DD ...)
        "closed_price": float,        # 实时参考价 (通知展示用)
        "suggested_delta": 下游填,
        "suggested_tenor_days": 下游填,
    }
    """
    if not isinstance(qqq_data, dict) or not qqq_data.get("closed_indicators_available"):
        return None

    rsi = qqq_data.get("closed_rsi")
    price = qqq_data.get("closed_price")
    if rsi is None or price is None:
        return None

    # 条件 1: RSI < 阈值
    if not (rsi < rsi_threshold):
        return None

    # 条件 2: 收盘价高于一年前 (52 周趋势过滤; 2026-10-11 起不再要求站上 SMA200)
    p1y = qqq_data.get("closed_price_1y_ago")
    if p1y is None or not (price > p1y):
        return None

    # closed_bar_date 是 pandas 时间戳, str() 会带 " 00:00:00-04:00" -> 只留日期
    raw_bar = qqq_data.get("closed_bar_date")
    if hasattr(raw_bar, "strftime"):
        bar_date = raw_bar.strftime("%Y-%m-%d")
    else:
        bar_date = str(raw_bar or "")[:10]

    return {
        "signal_base_price": float(price),
        "signal_rsi": float(rsi),
        "signal_bar_date": bar_date,
        "closed_price": qqq_data.get("last_price"),
    }


# ---------------------------------------------------------------------------
# 持仓监控 (退出线判定, 按评估优先级返回第一条命中的线)
# ---------------------------------------------------------------------------

def _trading_days_held(entry_date: date, today: date) -> Optional[int]:
    """自然日近似→交易日 (简单: 排除周末; 不含节假日, 与 yfinance 日历足够接近)。"""
    if entry_date is None or today is None:
        return None
    try:
        if today < entry_date:
            return 0
        days = 0
        d = entry_date
        while d < today:
            d += timedelta(days=1)
            if d.weekday() < 5:
                days += 1
        return days
    except Exception:
        return None


def evaluate_position(position: Any, qqq_data: Dict[str, Any], config: Any,
                      today: Optional[date] = None) -> Dict[str, Any]:
    """
    对一个 HOLDING 仓位评估退出线。

    返回:
    {
        "holding_days": int|None,      # 交易日
        "dte": int|None,               # 自然日
        "pnl_pct": float|None,         # 相对 total_cost (per-unit 口径: (premium-entry)/entry)
        "breach": None | {"type": "RSI_TP"|"TIME_STOP"|"DTE_FORCE", ...},
        "add_trigger": None | {"level": float, "next_count": int},
    }
    """
    today = today or datetime.now().date()
    result: Dict[str, Any] = {
        "holding_days": None,
        "dte": None,
        "pnl_pct": None,
        "breach": None,
        "add_trigger": None,
        "half_tp_trigger": None,
    }

    entry_date = getattr(position, "entry_date", None)
    if isinstance(entry_date, str):
        try:
            entry_date = date.fromisoformat(entry_date)
        except ValueError:
            entry_date = None

    exp = getattr(position, "expiration_date", None)
    if isinstance(exp, str):
        try:
            exp = date.fromisoformat(exp)
        except ValueError:
            exp = None

    result["holding_days"] = _trading_days_held(entry_date, today)
    if exp is not None:
        result["dte"] = (exp - today).days

    # --- PnL% (优先用实时权利金; 无报价时用 QQQ 涨跌幅粗估, 仅展示) ---
    entry = getattr(position, "entry_price", None)
    cur = getattr(position, "current_premium", None)
    if entry and cur and entry > 0:
        result["pnl_pct"] = (cur - entry) / entry * 100.0

    # --- 退出线 1: RSI 止盈 (基于已收盘 bar RSI, 收盘确认制) ---
    tp_rsi = config.get_tp_rsi()
    rsi_now = qqq_data.get("closed_rsi") if qqq_data.get("closed_indicators_available") else qqq_data.get("rsi")
    if rsi_now is not None and tp_rsi and rsi_now > tp_rsi:
        result["breach"] = {
            "type": "RSI_TP",
            "rsi": float(rsi_now),
            "tp_rsi": float(tp_rsi),
            "reason": f"RSI {rsi_now:.1f} > 止盈阈值 {tp_rsi:.0f}",
        }
        return result

    # --- 退出线 2: 时间止损 (持仓 N 个交易日仍未回本) ---
    ts_days = config.get_time_stop_trading_days()
    hd = result["holding_days"]
    if ts_days and hd is not None and hd >= ts_days:
        underwater = result["pnl_pct"] is None or result["pnl_pct"] < 0
        if underwater:
            result["breach"] = {
                "type": "TIME_STOP",
                "holding_days": hd,
                "time_stop_days": ts_days,
                "reason": f"持仓 {hd} 个交易日 (>= {ts_days}) 且未回本",
            }
            return result

    # --- 退出线 3: DTE 强制平仓 (硬风控, 无论盈亏) ---
    dte_force = config.get_dte_force_days()
    dte = result["dte"]
    if dte_force and dte is not None and dte <= dte_force:
        result["breach"] = {
            "type": "DTE_FORCE",
            "dte": dte,
            "dte_force_days": dte_force,
            "reason": f"距到期仅 {dte} 天 (<= {dte_force})",
        }
        return result

    # --- 加仓判定 (仅未触发退出线时) ---
    add_trigger = _check_add_trigger(position, qqq_data, config)
    if add_trigger:
        result["add_trigger"] = add_trigger

    # --- 分批止盈提醒 (HALF_TP): 总盈利≥阈值 且 尚未提醒 且 张数≥2 (仅提醒, 不改状态) ---
    half_tp = config.get_half_tp_pnl() if config is not None and hasattr(config, "get_half_tp_pnl") else None
    pnl_pct = result.get("pnl_pct")
    qty = int(getattr(position, "quantity", 0) or 0)
    if (half_tp and half_tp > 0 and pnl_pct is not None
            and pnl_pct / 100.0 >= half_tp and qty >= 2
            and not int(getattr(position, "half_tp_alerted", 0) or 0)):
        result["half_tp_trigger"] = {
            "pnl_pct": float(pnl_pct),
            "threshold": float(half_tp),
            "quantity": qty,
            "sell_qty": qty // 2,
            "reason": f"总盈利 {pnl_pct:+.1f}% ≥ +{half_tp*100:.0f}%, 建议卖出一半 ({qty // 2}/{qty} 张) 锁定利润",
        }

    return result


def _check_add_trigger(position: Any, qqq_data: Dict[str, Any], config: Any) -> Optional[Dict[str, Any]]:
    """加仓判定: 回撤达下一档位 + RSI 仍在入场区 + 未达最大张数。"""
    try:
        from app.market.option_math import effective_max_contracts

        levels = config.get_add_levels()
        # 张数上限 = min(硬上限, 资金额度/单张成本); 0 = 不限 (2026-10-11 起默认由额度决定)
        max_q = effective_max_contracts(
            config.get_max_quantity(), config.get_capital_cap(),
            float(getattr(position, "entry_price", 0) or 0))
        entry_rsi = config.get_entry_rsi_threshold()

        qty = int(getattr(position, "quantity", 0) or 0)
        add_count = int(getattr(position, "add_count", 0) or 0)
        if qty <= 0 or (max_q and qty >= max_q):
            return None
        if add_count >= len(levels):
            return None

        # 只允许按档位顺序加仓 (第 add_count 次加仓对应 levels[add_count])
        base = getattr(position, "signal_base_price", None)
        cur = qqq_data.get("closed_price") if qqq_data.get("closed_indicators_available") else qqq_data.get("last_price")
        rsi_now = qqq_data.get("closed_rsi") if qqq_data.get("closed_indicators_available") else qqq_data.get("rsi")
        if not base or not cur or rsi_now is None:
            return None

        level = levels[add_count]
        if cur <= base * (1 - level) and rsi_now < entry_rsi:
            return {
                "level": level,
                "next_count": qty + 1,
                "reason": f"较信号基准回撤 {(cur/base-1)*100:.1f}% (档位 -{level*100:.0f}%) 且 RSI {rsi_now:.1f} < {entry_rsi:.0f}",
            }
    except Exception as e:
        logger.warning(f"[WARN] add trigger check failed: {e}")
    return None


# ---------------------------------------------------------------------------
# 期权报价跟踪 (yfinance 期权链, 尽力而为; 失败不影响退出判定)
# ---------------------------------------------------------------------------

# 期权链报价缓存 (2026-10-11 评审 P2-3): 入场建议与持仓刷新复用同一次链请求,
# 避免在监控线程里反复下载整条链 (冷取实测 ~4s, 会推迟提醒发出)。
_PREMIUM_CACHE: Dict[Any, Any] = {}
_PREMIUM_CACHE_LOCK = threading.Lock()
PREMIUM_CACHE_TTL_SECONDS = 600
# 中间价可信度门槛 (评审 P2-2): 价差 / 中间价 超过该比例则认为盘口太虚, 退回成交价
PREMIUM_MAX_SPREAD_RATIO = 0.25


def _premium_cache_get(key):
    now = time.time()
    with _PREMIUM_CACHE_LOCK:
        item = _PREMIUM_CACHE.get(key)
        if not item:
            return None
        ts, value = item
        if now - ts > PREMIUM_CACHE_TTL_SECONDS:
            _PREMIUM_CACHE.pop(key, None)
            return None
        return value


def _premium_cache_put(key, value) -> None:
    with _PREMIUM_CACHE_LOCK:
        _PREMIUM_CACHE[key] = (time.time(), value)


def clear_premium_cache() -> None:
    """测试/排障用: 清空期权链缓存。"""
    with _PREMIUM_CACHE_LOCK:
        _PREMIUM_CACHE.clear()


def fetch_option_premium(strike: float, expiration: date, today: Optional[date] = None,
                         use_cache: bool = True) -> Optional[float]:
    """
    从 yfinance 期权链取指定行权价 call 的报价 (每股价)。

    取价优先级 (2026-10-11 评审 P2-2): 买卖中间价 (要求 bid>0 且价差 ≤ 25%) >
    最后成交价 lastPrice > regularMarketPrice。LEAPS 流动性差, 只看 lastPrice
    可能取到几小时前的成交价, 会把建议张数算偏。
    约定: strike 为 None/0 时自动按 ≈spot*0.94 选行权价 (0.70Δ 一年期近似, 见 option_math)。
    链上无该到期日或链为空时返回 None; 结果带 10 分钟进程内缓存 (P2-3)。
    报价仅用于展示/告警富化与建议张数, 退出判定不依赖。
    """
    cache_key = None
    if use_cache:
        try:
            cache_key = (round(float(strike or 0), 2), expiration.strftime("%Y-%m-%d"))
            cached = _premium_cache_get(cache_key)
            if cached is not None:
                return cached
        except Exception:
            cache_key = None

    try:
        import yfinance as yf

        t = yf.Ticker("QQQ")
        exp_str = expiration.strftime("%Y-%m-%d")
        chain = t.option_chain(exp_str)
        calls = chain.calls
        if calls is None or calls.empty:
            return None

        spot = None
        try:
            spot = float(t.fast_info["last_price"])
        except Exception:
            spot = None
        if not spot:
            return None

        # 目标行权价: 无指定时取 spot*0.94 附近最接近的 (0.70Δ/1 年 近似)
        target = float(strike) if strike and strike > 0 else spot * 0.94
        calls = calls.assign(_dist=(calls["strike"] - target).abs())
        row = calls.sort_values("_dist").iloc[0]

        price = None
        # 1) 买卖中间价 (盘口不能太虚)
        try:
            bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
            if bid > 0 and ask > 0 and ask >= bid:
                mid = (bid + ask) / 2.0
                if mid > 0 and (ask - bid) / mid <= PREMIUM_MAX_SPREAD_RATIO:
                    price = mid
        except Exception:
            price = None
        # 2) 最后成交价
        if price is None:
            for col in ("lastPrice", "regularMarketPrice"):
                v = row.get(col)
                try:
                    if v is not None and float(v) > 0:
                        price = float(v)
                        break
                except (TypeError, ValueError):
                    continue
        # 3) 盘口太虚时仍用中间价兜底 (好过没有)
        if price is None:
            try:
                bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
                if bid > 0 and ask > 0:
                    price = (bid + ask) / 2.0
            except Exception:
                price = None

        if price is not None and cache_key is not None:
            _premium_cache_put(cache_key, float(price))
        return float(price) if price else None
    except Exception as e:
        logger.debug(f"[DEBUG] option premium fetch failed: {e}")
        return None
