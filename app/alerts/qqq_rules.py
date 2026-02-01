from typing import Dict, List, Optional, Tuple
from datetime import datetime
from pytz import timezone

et_tz = timezone("America/New_York")


def check_panic_acceleration(indicators: Dict, vix_data: Dict) -> Dict:
    """
    检测恐慌加速度（仅在 Level 2/3 信号触发时调用）
    
    满足任意 2/3 条件即判定为恐慌加速度成立
    
    条件 A: 成交量放大 - Volume > MA20(Volume) × 1.5
    条件 B: 跌幅集中 - 最近 3 天中至少 2 天跌幅 ≤ -1.5%
    条件 C: VIX 暴涨 - VIX 单日涨幅 ≥ +15% 或绝对涨幅 ≥ 3 点
    
    Returns:
        Dict containing:
        - is_panic: 是否满足恐慌加速度
        - conditions_met: 满足条件数
        - condition_a: (满足?, 描述)
        - condition_b: (满足?, 描述)
        - condition_c: (满足?, 描述)
    """
    conditions_met = 0
    
    # 条件 A: 成交量放大
    volume = indicators.get("volume")
    volume_ma20 = indicators.get("volume_ma20")
    
    if volume and volume_ma20 and volume_ma20 > 0:
        volume_ratio = volume / volume_ma20
        condition_a_met = volume_ratio > 1.5
        condition_a_desc = f"{volume_ratio:.1f}x MA20"
    else:
        condition_a_met = False
        condition_a_desc = "数据不可用"
    
    if condition_a_met:
        conditions_met += 1
    
    # 条件 B: 跌幅集中
    daily_changes = indicators.get("daily_changes", [])
    days_with_big_drop = sum(1 for change in daily_changes if change <= -1.5)
    total_days = len(daily_changes)
    
    if total_days >= 3:
        condition_b_met = days_with_big_drop >= 2
        condition_b_desc = f"{days_with_big_drop}/{total_days}天 跌>1.5%"
    else:
        condition_b_met = False
        condition_b_desc = f"数据不足({total_days}天)"
    
    if condition_b_met:
        conditions_met += 1
    
    # 条件 C: VIX 暴涨
    vix_change_pct = vix_data.get("vix_change_pct", 0)
    vix_change_abs = vix_data.get("vix_change_abs", 0)
    
    if vix_data:
        condition_c_met = vix_change_pct >= 15 or vix_change_abs >= 3.0
        condition_c_desc = f"+{vix_change_pct:.1f}% (+{vix_change_abs:.1f}点)"
    else:
        condition_c_met = False
        condition_c_desc = "VIX数据不可用"
    
    if condition_c_met:
        conditions_met += 1
    
    return {
        "is_panic": conditions_met >= 2,
        "conditions_met": conditions_met,
        "condition_a": (condition_a_met, condition_a_desc),
        "condition_b": (condition_b_met, condition_b_desc),
        "condition_c": (condition_c_met, condition_c_desc)
    }


def recommend_delta_by_vix(vix_data: Dict) -> Dict:
    """
    基于 VIX/MA20 比值推荐 Delta 档位
    
    VIX_Ratio ≤ 1.3  → 安全区（低 IV）  → Delta 0.60-0.65
    VIX_Ratio ≤ 1.5  → 警告区（中 IV）  → Delta 0.70-0.75
    VIX_Ratio > 1.5  → 危险区（高 IV）  → Delta ≥ 0.85
    
    Returns:
        Dict containing:
        - vix_current: VIX 当前值
        - vix_ma20: VIX MA20
        - vix_ratio: VIX/MA20 比值
        - iv_zone: IV 区域名称
        - delta_recommend: Delta 推荐值
        - explanation: 说明
        - available: 数据是否可用
    """
    if not vix_data or not vix_data.get("vix_current"):
        return {
            "available": False,
            "vix_current": None,
            "vix_ma20": None,
            "vix_ratio": None,
            "iv_zone": "N/A",
            "delta_recommend": "N/A",
            "explanation": "VIX 数据不可用，建议默认 Delta 0.60"
        }
    
    vix_current = vix_data["vix_current"]
    vix_ma20 = vix_data["vix_ma20"]
    vix_ratio = vix_data["vix_ratio"]
    
    if vix_ratio <= 1.3:
        iv_zone = "安全区（低 IV）"
        delta_recommend = "0.60 - 0.65"
        explanation = "低 IV 环境，轻度 ITM 即可平衡时间价值与爆发力"
    elif vix_ratio <= 1.5:
        iv_zone = "警告区（中 IV）"
        delta_recommend = "0.70 - 0.75"
        explanation = "中等 IV 环境，适度 ITM 降低 Vega 风险"
    else:
        iv_zone = "危险区（高 IV）"
        delta_recommend = "≥ 0.85"
        explanation = "高 IV 环境，Deep ITM 最小化波动率风险（IV Crush 防护）"
    
    return {
        "available": True,
        "vix_current": vix_current,
        "vix_ma20": vix_ma20,
        "vix_ratio": vix_ratio,
        "iv_zone": iv_zone,
        "delta_recommend": delta_recommend,
        "explanation": explanation
    }


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

    # 计算当日跌幅供参考
    daily_drop_pct = (current_price - prev_close) / prev_close * 100

    # 2. 分级信号
    
    # Level 1: 轻度回调
    if config.is_entry_level1_enabled():
        dist_ma20_pct = abs(current_price - ma20) / ma20 * 100
        
        if daily_drop_pct <= -1.2 and dist_ma20_pct <= 0.5:
            alerts.append({
                "rule_name": "Level 1 Entry",
                "message": f"{bear_prefix}🟢 [日常回调] 跌幅 {daily_drop_pct:.2f}%, 触碰 MA20",
                "trigger_condition": f"跌幅 {daily_drop_pct:.2f}% <= -1.2% AND MA20距离 {dist_ma20_pct:.2f}% <= 0.5%",
                "severity": "LOW",
                "alert_type": "QQQ_ENTRY_L1",
                "current_price": current_price,
                "drop_percent": daily_drop_pct
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
                "alert_type": "QQQ_ENTRY_L2",
                "current_price": current_price,
                "drop_percent": three_day_drop_pct
            })

    # Level 3: 极端超卖
    if config.is_entry_level3_enabled():
        if current_price < bb_lower:
            alerts.append({
                "rule_name": "Level 3 Entry",
                "message": f"{bear_prefix}📉 [极端超卖] 价格跌破布林下轨",
                "trigger_condition": f"价格 {current_price:.2f} < BB Lower {bb_lower:.2f}",
                "severity": "CRITICAL",
                "alert_type": "QQQ_ENTRY_L3",
                "current_price": current_price,
                "drop_percent": daily_drop_pct
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
