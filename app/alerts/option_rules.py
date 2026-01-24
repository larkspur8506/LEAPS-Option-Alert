from typing import Dict, List, Optional
from datetime import datetime, date
from pytz import timezone

et_tz = timezone("America/New_York")


def check_max_holding_period(position, current_price: float, config) -> Optional[Dict]:
    today = datetime.now(et_tz).date()
    days_held = (today - position.entry_date).days
    max_days = config.get_max_holding_days()

    if days_held >= max_days:
        return {
            "alert_type": "OPTION_MAX_HOLDING",
            "rule_name": "Max Holding Period",
            "message": f"该期权已达到/超过最大持仓周期 ({max_days}天)",
            "trigger_condition": f"持仓天数 {days_held} >= 阈值 {max_days}",
            "severity": "MEDIUM",
            "position_id": position.id,
            "current_price": current_price,
            "entry_price": position.entry_price,
            "days_held": days_held,
            "max_days": max_days,
            "timestamp": datetime.now(et_tz)
        }

    return None


def check_take_profit(position, current_price: float, config) -> Optional[Dict]:
    if current_price is None:
        return None

    entry_price = position.entry_price
    profit_pct = (current_price - entry_price) / entry_price

    days_held = (datetime.now(et_tz).date() - position.entry_date).days

    phase1_days = config.get_take_profit_phase1_days()
    phase2_days = config.get_take_profit_phase2_days()

    phase1_threshold = config.get_take_profit_phase1_threshold()
    phase2_threshold = config.get_take_profit_phase2_threshold()
    phase3_threshold = config.get_take_profit_phase3_threshold()

    rule_name = None
    threshold_pct = None

    if days_held <= phase1_days:
        if profit_pct >= phase1_threshold:
            rule_name = "Take Profit Phase 1"
            threshold_pct = phase1_threshold
    elif days_held <= phase2_days:
        if profit_pct >= phase2_threshold:
            rule_name = "Take Profit Phase 2"
            threshold_pct = phase2_threshold
    else:
        if profit_pct >= phase3_threshold:
            rule_name = "Take Profit Phase 3"
            threshold_pct = phase3_threshold

    qty = position.quantity or 1
    profit_amount = (current_price - entry_price) * qty

    if rule_name:
        return {
            "alert_type": "OPTION_TAKE_PROFIT",
            "rule_name": rule_name,
            "message": f"止盈提醒: 已盈利 +{profit_pct*100:.1f}% (+${profit_amount:.2f}) ({rule_name})",
            "trigger_condition": f"当前盈利 +{profit_pct*100:.1f}% >= 阈值 +{threshold_pct*100:.1f}%",
            "severity": "MEDIUM",
            "position_id": position.id,
            "current_price": current_price,
            "entry_price": entry_price,
            "profit_pct": profit_pct * 100,
            "profit_amount": profit_amount,
            "threshold_pct": threshold_pct * 100,
            "days_held": days_held,
            "timestamp": datetime.now(et_tz)
        }

    return None


def check_stop_loss(position, current_price: float, config) -> Optional[Dict]:
    if current_price is None:
        return None

    entry_price = position.entry_price
    loss_pct = (current_price - entry_price) / entry_price

    stop_loss_threshold = -config.get_stop_loss_threshold()

    qty = position.quantity or 1
    loss_amount = (current_price - entry_price) * qty

    if loss_pct <= stop_loss_threshold:
        return {
            "alert_type": "OPTION_STOP_LOSS",
            "rule_name": "Stop Loss",
            "message": f"止损提醒: 已亏损 {loss_pct*100:.1f}% (${loss_amount:.2f})",
            "trigger_condition": f"当前亏损率 {loss_pct*100:.1f}% <= 止损阈值 {stop_loss_threshold*100:.1f}%",
            "severity": "HIGH",
            "position_id": position.id,
            "current_price": current_price,
            "entry_price": entry_price,
            "loss_pct": loss_pct * 100,
            "loss_amount": loss_amount,
            "threshold_pct": stop_loss_threshold * 100,
            "timestamp": datetime.now(et_tz)
        }

    return None


def check_dte_risk(position, config) -> Optional[Dict]:
    from datetime import timedelta

    dte = (position.expiration_date - datetime.now(et_tz).date()).days
    warning_days = config.get_dte_warning_days()

    if dte <= warning_days:
        return {
            "alert_type": "OPTION_TIME",
            "rule_name": "DTE Warning",
            "message": f"时间风险提醒: 剩余 {dte} 天到期",
            "trigger_condition": f"剩余天数 {dte} <= 阈值 {warning_days}",
            "severity": "MEDIUM",
            "position_id": position.id,
            "dte": dte,
            "expiration_date": position.expiration_date,
            "timestamp": datetime.now(et_tz)
        }

    return None


def check_all_option_rules(position, current_price: float, config) -> List[Dict]:
    alerts = []

    alert_max_holding = check_max_holding_period(position, current_price, config)
    if alert_max_holding:
        alerts.append(alert_max_holding)

    alert_take_profit = check_take_profit(position, current_price, config)
    if alert_take_profit:
        alerts.append(alert_take_profit)

    alert_stop_loss = check_stop_loss(position, current_price, config)
    if alert_stop_loss:
        alerts.append(alert_stop_loss)

    alert_dte = check_dte_risk(position, config)
    if alert_dte:
        alerts.append(alert_dte)

    return alerts


def format_position_ticker(position) -> str:
    exp_date = position.expiration_date.strftime("%y%m%d")
    option_type = "C" if position.option_type == "CALL" else "P"
    strike = int(position.strike_price)
    return f"{position.underlying}{exp_date}{option_type}{strike}"
