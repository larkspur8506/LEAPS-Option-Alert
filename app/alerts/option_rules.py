from typing import Dict, List, Optional, Any
from datetime import datetime, date
from pytz import timezone
import logging

et_tz = timezone("America/New_York")
logger = logging.getLogger(__name__)


def check_position_signals(position, current_opt_price: float, qqq_indicators: Dict, config=None) -> Dict[str, Any]:
    """
    重构后的期权出场/风控规则 - 长期复利引擎
    """
    alerts = []
    
    # 1. 数据准备 (防御性编程)
    try:
        entry_date = position.entry_date
        if isinstance(entry_date, str):
            entry_date = date.fromisoformat(entry_date)
            
        expiration_date = position.expiration_date
        if isinstance(expiration_date, str):
            expiration_date = date.fromisoformat(expiration_date)
            
        today = datetime.now(et_tz).date()
        held_days = (today - entry_date).days
        dte = (expiration_date - today).days
        
        entry_price = position.entry_price
        if entry_price <= 0:
            pnl_pct = 0.0
        else:
            pnl_pct = (current_opt_price - entry_price) / entry_price
            
        # 更新最高收益
        current_max_profit = getattr(position, "max_profit", 0.0) or 0.0
        new_max_profit = max(current_max_profit, pnl_pct)
        
    except Exception as e:
        logger.error(f"Error preparing data for position {position.id}: {e}")
        return {'alerts': [], 'new_max_profit': 0.0}

    # 4. 强制止损/时间风控 (Hard Stop)
    # 当期权合约距离到期日仅剩 6 个月 (约 180 天) 时，无论盈亏状态如何，必须强制平仓
    if dte <= 180:
        alerts.append({
            "rule_name": "Time Stop (180 DTE)",
            "message": f"⛔ [强制平仓] 距离到期日仅剩 {dte} 天 (<=180天)，触发时间风控",
            "severity": "CRITICAL",
            "trigger_condition": f"DTE {dte} <= 180",
            "alert_type": "OPTION_TIME",
            "dte": dte,
            "expiration_date": expiration_date.strftime("%Y-%m-%d")
        })
    else:
        # 3. 出场逻辑 (Exit/Profit Taking) - 阶梯止盈
        tp_threshold = None
        duration_desc = ""
        
        if held_days < 365:
            tp_threshold = 1.00  # 100%
            duration_desc = "< 12 个月"
        elif 365 <= held_days <= 456:
            tp_threshold = 0.50  # 50%
            duration_desc = "12-15 个月"
        elif 456 < held_days <= 547:
            tp_threshold = 0.30  # 30%
            duration_desc = "16-18 个月"
        else:
            tp_threshold = 0.30  # 默认兜底
            duration_desc = "> 18 个月"
            
        if tp_threshold is not None and pnl_pct >= tp_threshold:
            alerts.append({
                "rule_name": "Tiered Take Profit",
                "message": f"🎯 [阶梯止盈] 持仓 {duration_desc}，收益达标 ({tp_threshold*100:.0f}%)",
                "severity": "HIGH",
                "trigger_condition": f"持仓 {held_days}天 ({duration_desc}) AND 盈利 {pnl_pct*100:.1f}% >= {tp_threshold*100:.0f}%",
                "alert_type": "OPTION_TAKE_PROFIT",
                "profit_pct": pnl_pct * 100,
                "days_held": held_days
            })

    # Formatting alerts
    for alert in alerts:
        alert["position_id"] = position.id
        alert["entry_price"] = entry_price
        alert["current_price"] = current_opt_price
        alert["pnl_pct"] = pnl_pct * 100
        alert["timestamp"] = datetime.now(et_tz)
    
    return {
        "alerts": alerts,
        "new_max_profit": new_max_profit
    }


def format_position_ticker(position) -> str:
    """Helper to format ticker for notifications"""
    try:
        exp_date_obj = position.expiration_date
        if isinstance(exp_date_obj, str):
            exp_date_obj = date.fromisoformat(exp_date_obj)
            
        exp_date = exp_date_obj.strftime("%y%m%d")
        option_type = "C" if position.option_type == "CALL" else "P"
        strike = int(position.strike_price)
        return f"{position.underlying}{exp_date}{option_type}{strike}"
    except Exception:
        return f"{position.underlying}-OPT"
