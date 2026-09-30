"""
Nasdaq-100 (NDX) Entry Rules and Suggested Grid Generator.
"""
from typing import Dict, List, Optional, Any
from datetime import datetime
from pytz import timezone
from app.alerts.grid_math import calculate_grid_parameters

et_tz = timezone("America/New_York")


def _entry_reference(current_price: float, indicators: Dict[str, Any]) -> Dict[str, Any]:
    """
    选择开仓判定的参考基准 (收盘确认制)。

    优先使用数据层给出的"最后一根已收盘 bar"指标 (closed_*), 因为盘中最后一根
    日K仍未收盘, 用它判定会被盘中噪声左右。缺失 closed_* 时退回既有实时字段,
    保持与历史版本完全一致的语义 (兼容旧数据/测试)。

    返回:
        {"basis": "CLOSED_BAR"|"LIVE_BAR", "price": float, "rsi": ..., "is_above_sma200_3d": ...,
         "price_1y_ago": ..., "bar_date": str|None}
    """
    closed_available = bool(indicators.get("closed_indicators_available"))
    closed_price = indicators.get("closed_price")
    if closed_available and closed_price is not None:
        return {
            "basis": "CLOSED_BAR",
            "price": float(closed_price),
            "rsi": indicators.get("closed_rsi"),
            "is_above_sma200_3d": indicators.get("closed_is_above_sma200_3d", False),
            "price_1y_ago": indicators.get("closed_price_1y_ago"),
            "bar_date": indicators.get("closed_bar_date"),
        }
    return {
        "basis": "LIVE_BAR",
        "price": float(current_price) if current_price else current_price,
        "rsi": indicators.get("rsi"),
        "is_above_sma200_3d": indicators.get("is_above_sma200_3d", False),
        "price_1y_ago": indicators.get("price_1y_ago"),
        "bar_date": indicators.get("data_timestamp"),
    }


def check_ndx_entry_conditions(
    current_price: float,
    indicators: Dict[str, Any],
    rsi_threshold: float = 35.0
) -> bool:
    """
    检查 NDX 是否满足做多网格开仓三大条件:
    1. RSI14 < rsi_threshold (默认 35.0)
    2. 最近连续 3 个交易日收盘价 > SMA200 (is_above_sma200_3d == True)
    3. 参考收盘价 > 约一年前(252交易日前)收盘价 (reference_price > price_1y_ago)

    参考基准为**最后一根已收盘 bar**(收盘确认制): 盘中不因实时 RSI 瞬时跌破阈值
    而触发开仓信号。数据层未提供 closed_* 字段时退回实时字段 (历史语义)。

    返回:
        bool: 三个条件全部满足时返回 True，否则返回 False
    """
    if current_price is None or current_price <= 0:
        return False

    ref = _entry_reference(current_price, indicators)

    rsi = ref["rsi"]
    if rsi is None or rsi >= rsi_threshold:
        return False

    if not ref["is_above_sma200_3d"]:
        return False

    price_1y_ago = ref["price_1y_ago"]
    if price_1y_ago is None or ref["price"] <= price_1y_ago:
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

    ref = _entry_reference(current_price, indicators)
    rsi = ref["rsi"]
    price_1y_ago = ref["price_1y_ago"]
    basis_note = "收盘确认" if ref["basis"] == "CLOSED_BAR" else "实时(未收盘)"
    bar_date = ref.get("bar_date")

    alert = {
        "rule_name": "NDX Grid Entry Signal",
        "ticker": "^NDX",
        "message": (
            f"🚨 [NDX网格开仓机会] RSI跌破{rsi_threshold:.0f} ({rsi:.1f})，连续3天站上SMA200，"
            f"参考收盘价({ref['price']:.2f})高于1年前({price_1y_ago:.2f})"
            f"[{basis_note}基准]"
        ),
        "trigger_condition": f"RSI < {rsi_threshold} AND NDX > SMA200(3d) AND Price > 1y_ago",
        "severity": "CRITICAL",
        "alert_type": "NDX_GRID_ENTRY",
        "current_price": current_price,
        "reference_price": ref["price"],
        "entry_basis": ref["basis"],
        "reference_bar_date": bar_date,
        "price_1y_ago": price_1y_ago,
        "rsi": rsi,
        "suggested_grid": suggested_grid,
        "timestamp": datetime.now(et_tz)
    }

    return [alert]


# 别名兼容
check_ndx_entry_signals = check_entry_signals

