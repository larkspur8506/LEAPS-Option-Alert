from typing import Dict, List
from datetime import datetime
from pytz import timezone

et_tz = timezone("America/New_York")

def check_entry_signals(current_price: float, indicators: Dict, config) -> List[Dict]:
    """
    重构后的 QQQ 长期复利引擎入场规则
    """
    alerts = []
    
    rsi = indicators.get('rsi')
    
    # 必须数据检查
    if not rsi:
        return []

    is_above_sma200_3d = indicators.get("is_above_sma200_3d", False)
    price_1y_ago = indicators.get("price_1y_ago")

    # 触发条件：RSI < 35 且 连续3天收盘价大于SMA200 且 当前价高于一年前收盘价
    if rsi < 35 and is_above_sma200_3d and (price_1y_ago is not None and current_price > price_1y_ago):
        alerts.append({
            "rule_name": "RSI Oversold + SMA200 Trend Entry",
            "message": f"🚨 [入场机会] RSI跌破35 ({rsi:.1f})，且连续3天站上SMA200，且现价({current_price:.2f})高于1年前({price_1y_ago:.2f})",
            "trigger_condition": f"RSI < 35 AND QQQ > SMA200(3d) AND Price > 1y_ago",
            "severity": "CRITICAL",
            "alert_type": "QQQ_ENTRY",
            "current_price": current_price,
            "drop_percent": 0.0,
            "delta_recommendation": {
                "available": True,
                "delta_recommend": "≈ 0.6",
                "expiration": "约 12 个月 (365天左右)",
                "explanation": "策略要求：选择距离现在约 12 个月到期的合约，Delta 接近 0.6 的深度实值期权。"
            }
        })

    # Add timestamp to all
    for alert in alerts:
        alert["timestamp"] = datetime.now(et_tz)

    return alerts


def check_all_qqq_rules(qqq_data: Dict, config) -> List[Dict]:
    """
    Main entry point for QQQ checks
    """
    current_price = qqq_data.get("last_price")
    if not current_price:
        return []

    return check_entry_signals(current_price, qqq_data, config)
