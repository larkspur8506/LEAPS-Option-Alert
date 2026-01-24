from typing import Dict, List, Optional
from datetime import datetime
from pytz import timezone

et_tz = timezone("America/New_York")


def check_qqq_rule_a(qqq_data: Dict) -> Optional[Dict]:
    prev_close = qqq_data.get("prev_close")
    current_price = qqq_data.get("last_price")

    if not prev_close or not current_price:
        return None

    threshold = prev_close * 0.98

    if current_price <= threshold:
        drop_pct = (current_price - prev_close) / prev_close * 100
        return {
            "rule_name": "Rule A",
            "message": f"QQQ 价格跌破昨日收盘价 2%，当前价格: ${current_price:.2f}",
            "trigger_condition": f"当前价 ${current_price:.2f} <= 阈值 ${threshold:.2f}",
            "severity": "HIGH",
            "drop_percent": drop_pct,
            "timestamp": datetime.now(et_tz)
        }

    return None


def check_qqq_rule_b(qqq_data: Dict) -> Optional[Dict]:
    intraday_high = qqq_data.get("intraday_high")
    current_price = qqq_data.get("last_price")

    if not intraday_high or not current_price:
        return None

    threshold = intraday_high * 0.98

    if current_price <= threshold:
        drop_pct = (current_price - intraday_high) / intraday_high * 100
        return {
            "rule_name": "Rule B",
            "message": f"QQQ 价格跌破当日最高价 2%，当前价格: ${current_price:.2f}",
            "trigger_condition": f"当前价 ${current_price:.2f} <= 阈值 ${threshold:.2f}",
            "severity": "HIGH",
            "drop_percent": drop_pct,
            "timestamp": datetime.now(et_tz)
        }

    return None


def check_qqq_rule_c(qqq_data: Dict) -> Optional[Dict]:
    close_2_days_ago = qqq_data.get("close_2_days_ago")
    current_price = qqq_data.get("last_price")

    if not close_2_days_ago or not current_price:
        return None

    threshold = close_2_days_ago * 0.98

    if current_price <= threshold:
        drop_pct = (current_price - close_2_days_ago) / close_2_days_ago * 100
        return {
            "rule_name": "Rule C",
            "message": f"QQQ 价格较2日前收盘价下跌2%以上，当前价格: ${current_price:.2f}",
            "trigger_condition": f"当前价 ${current_price:.2f} <= 阈值 ${threshold:.2f}",
            "severity": "MEDIUM",
            "drop_percent": drop_pct,
            "timestamp": datetime.now(et_tz)
        }

    return None


def check_qqq_rule_d(qqq_data: Dict) -> Optional[Dict]:
    rolling_high_3d = qqq_data.get("rolling_high_3d")
    current_price = qqq_data.get("last_price")

    if not rolling_high_3d or not current_price:
        return None

    threshold = rolling_high_3d * 0.98

    if current_price <= threshold:
        drop_pct = (current_price - rolling_high_3d) / rolling_high_3d * 100
        return {
            "rule_name": "Rule D",
            "message": f"QQQ 价格跌破3日滚动高点 2%，当前价格: ${current_price:.2f}",
            "trigger_condition": f"当前价 ${current_price:.2f} <= 阈值 ${threshold:.2f}",
            "severity": "MEDIUM",
            "drop_percent": drop_pct,
            "timestamp": datetime.now(et_tz)
        }

    return None


def check_all_qqq_rules(qqq_data: Dict, config) -> List[Dict]:
    alerts = []

    if config.is_qqq_rule_a_enabled():
        alert_a = check_qqq_rule_a(qqq_data)
        if alert_a:
            alerts.append(alert_a)

    if config.is_qqq_rule_b_enabled():
        alert_b = check_qqq_rule_b(qqq_data)
        if alert_b:
            alerts.append(alert_b)

    if config.is_qqq_rule_c_enabled():
        alert_c = check_qqq_rule_c(qqq_data)
        if alert_c:
            alerts.append(alert_c)

    if config.is_qqq_rule_d_enabled():
        alert_d = check_qqq_rule_d(qqq_data)
        if alert_d:
            alerts.append(alert_d)

    return alerts
