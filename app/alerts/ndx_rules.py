"""
Nasdaq-100 (NDX) Entry Rules and Suggested Grid Generator.
"""
from typing import Dict, List, Optional, Any
from datetime import datetime
from pytz import timezone
from app.alerts.grid_math import calculate_grid_parameters

et_tz = timezone("America/New_York")


def check_ndx_entry_conditions(
    current_price: float,
    indicators: Dict[str, Any],
    rsi_threshold: float = 35.0
) -> bool:
    """
    检查 NDX 是否满足做多网格开仓三大条件:
    1. RSI14 < rsi_threshold (默认 35.0)
    2. 最近连续 3 个交易日收盘价 > SMA200 (is_above_sma200_3d == True)
    3. 当前价格 > 约一年前(252交易日前)收盘价 (current_price > price_1y_ago)
    
    返回:
        bool: 三个条件全部满足时返回 True，否则返回 False
    """
    if current_price is None or current_price <= 0:
        return False

    rsi = indicators.get("rsi")
    if rsi is None or rsi >= rsi_threshold:
        return False

    is_above_sma200_3d = indicators.get("is_above_sma200_3d", False)
    if not is_above_sma200_3d:
        return False

    price_1y_ago = indicators.get("price_1y_ago")
    if price_1y_ago is None or current_price <= price_1y_ago:
        return False

    return True


def generate_suggested_grid(
    current_price: float,
    upper_pct: float = 0.20,
    lower_pct: float = 0.20,
    grid_count: int = 200,
    leverage: float = 5.0
) -> Dict[str, Any]:
    """
    以当前价格作为基准价，生成做多等差网格建议参数。
    """
    return calculate_grid_parameters(
        base_price=current_price,
        upper_pct=upper_pct,
        lower_pct=lower_pct,
        grid_count=grid_count,
        leverage=leverage
    )


def check_entry_signals(
    current_price: float,
    indicators: Dict[str, Any],
    config=None
) -> List[Dict[str, Any]]:
    """
    开仓信号生成主函数。
    当满足开仓条件时，输出标准信号结构体，包含建议网格参数。
    """
    if not current_price or current_price <= 0:
        return []

    # 支持从 config 中读取阈值，若无则使用默认值
    rsi_threshold = 35.0
    upper_pct = 0.20
    lower_pct = 0.20
    grid_count = 200
    leverage = 5.0

    if config:
        if hasattr(config, "get_rsi_threshold"):
            rsi_threshold = config.get_rsi_threshold()
        if hasattr(config, "get_default_grid_upper_pct"):
            upper_pct = config.get_default_grid_upper_pct()
        if hasattr(config, "get_default_grid_lower_pct"):
            lower_pct = config.get_default_grid_lower_pct()
        if hasattr(config, "get_default_grid_count"):
            grid_count = config.get_default_grid_count()
        if hasattr(config, "get_default_grid_leverage"):
            leverage = config.get_default_grid_leverage()

    is_met = check_ndx_entry_conditions(current_price, indicators, rsi_threshold=rsi_threshold)
    if not is_met:
        return []

    suggested_grid = generate_suggested_grid(
        current_price=current_price,
        upper_pct=upper_pct,
        lower_pct=lower_pct,
        grid_count=grid_count,
        leverage=leverage
    )

    rsi = indicators.get("rsi")
    price_1y_ago = indicators.get("price_1y_ago")

    alert = {
        "rule_name": "NDX Grid Entry Signal",
        "ticker": "^NDX",
        "message": f"🚨 [NDX网格开仓机会] RSI跌破{rsi_threshold:.0f} ({rsi:.1f})，连续3天站上SMA200，现价({current_price:.2f})高于1年前({price_1y_ago:.2f})",
        "trigger_condition": f"RSI < {rsi_threshold} AND NDX > SMA200(3d) AND Price > 1y_ago",
        "severity": "CRITICAL",
        "alert_type": "NDX_GRID_ENTRY",
        "current_price": current_price,
        "price_1y_ago": price_1y_ago,
        "rsi": rsi,
        "suggested_grid": suggested_grid,
        "timestamp": datetime.now(et_tz)
    }

    return [alert]


# 别名兼容
check_ndx_entry_signals = check_entry_signals

