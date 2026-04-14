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

    # 触发条件：当 RSI 跌破 35 时（代表市场进入超卖区/恐慌期）
    if rsi < 35:
        alerts.append({
            "rule_name": "RSI Oversold Entry",
            "message": f"🚨 [超卖机会] RSI 跌破 35: 当前 RSI {rsi:.1f}",
            "trigger_condition": f"RSI {rsi:.1f} < 35",
            "severity": "CRITICAL",
            "alert_type": "QQQ_ENTRY",
            "current_price": current_price,
            "drop_percent": 0.0,
            "delta_recommendation": {
                "available": True,
                "delta_recommend": "≈ 0.70",
                "expiration": "约 2 年 (730天左右)",
                "explanation": "策略要求：选择距离现在约 2 年到期的合约，Delta 接近 0.7 的深度实值期权。"
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
