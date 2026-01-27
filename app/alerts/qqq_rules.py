from typing import Dict, List, Optional
from datetime import datetime
from pytz import timezone

et_tz = timezone("America/New_York")


def check_entry_signals(current_price: float, indicators: Dict, config) -> List[Dict]:
    """
    重构后的 QQQ 入场规则
    根据 config 开关决定是否生成对应级别的信号
    """
    alerts = []
    
    ma20 = indicators.get('ma20')
    ma200 = indicators.get('ma200')
    rsi = indicators.get('rsi')
    bb_upper = indicators.get('bb_upper')
    bb_lower = indicators.get('bb_lower')
    prev_close = indicators.get('prev_close')
    three_day_prev_close = indicators.get('three_day_prev_close')
    
    # 必须数据检查
    if not all([ma20, ma200, rsi, bb_upper, bb_lower, prev_close, three_day_prev_close]):
        return []

    # 1. 趋势判断
    is_bear_market = current_price < ma200
    bear_prefix = "⚠️ [熊市趋势] (价格低于年线) " if is_bear_market else ""

    # 2. 分级信号
    
    # Level 1: 轻度回调
    if config.is_entry_level1_enabled():
        daily_drop_pct = (current_price - prev_close) / prev_close * 100
        dist_ma20_pct = abs(current_price - ma20) / ma20 * 100
        
        if daily_drop_pct <= -1.2 and dist_ma20_pct <= 0.5:
            alerts.append({
                "rule_name": "Level 1 Entry",
                "message": f"{bear_prefix}🟢 [日常回调] 跌幅 {daily_drop_pct:.2f}%, 触碰 MA20",
                "trigger_condition": f"跌幅 {daily_drop_pct:.2f}% <= -1.2% AND MA20距离 {dist_ma20_pct:.2f}% <= 0.5%",
                "severity": "LOW",
                "alert_type": "QQQ_ENTRY_L1"
            })

    # Level 2: 黄金坑
    if config.is_entry_level2_enabled():
        three_day_drop_pct = (current_price - three_day_prev_close) / three_day_prev_close * 100
        
        if three_day_drop_pct <= -3.5 and rsi < 32:
            alerts.append({
                "rule_name": "Level 2 Entry",
                "message": f"{bear_prefix}🚨 [黄金坑机会] 3日跌幅 {three_day_drop_pct:.2f}%, RSI {rsi:.1f}",
                "trigger_condition": f"3日跌幅 {three_day_drop_pct:.2f}% <= -3.5% AND RSI {rsi:.1f} < 32",
                "severity": "HIGH",
                "alert_type": "QQQ_ENTRY_L2"
            })

    # Level 3: 极端超卖
    if config.is_entry_level3_enabled():
        if current_price < bb_lower:
            alerts.append({
                "rule_name": "Level 3 Entry",
                "message": f"{bear_prefix}📉 [极端超卖] 价格跌破布林下轨",
                "trigger_condition": f"价格 {current_price:.2f} < BB Lower {bb_lower:.2f}",
                "severity": "CRITICAL",
                "alert_type": "QQQ_ENTRY_L3"
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

    # 使用新的逻辑，传入 config
    return check_entry_signals(current_price, qqq_data, config)
