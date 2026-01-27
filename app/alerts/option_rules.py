from typing import Dict, List, Optional, Any
from datetime import datetime, date
from pytz import timezone
import logging

et_tz = timezone("America/New_York")
logger = logging.getLogger(__name__)


def check_position_signals(position, current_opt_price: float, qqq_indicators: Dict, config=None) -> Dict[str, Any]:
    """
    重构后的期权出场/风控规则
    根据 config 开关决定是否生成对应规则的信号
    
    返回值: {'alerts': [], 'new_max_profit': float}
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

    # Helper: 检查开关是否启用 (兼容 config 为 None 的情况)
    def is_enabled(check_func_name: str) -> bool:
        if config is None:
            return True
        check_func = getattr(config, check_func_name, None)
        if check_func:
            return check_func()
        return True

    # 2. 止盈规则
    
    # 硬性止盈: >= 50%
    if is_enabled('is_exit_hard_tp_enabled') and pnl_pct >= 0.50:
        alerts.append({
            "rule_name": "Hard Take Profit",
            "message": "🎯 [目标达成] 收益达到 50%",
            "severity": "HIGH",
            "trigger_condition": f"盈利 {pnl_pct*100:.1f}% >= 50%"
        })
        
    # 极速止盈: 持仓 <= 7天 AND 收益 >= 15%
    if is_enabled('is_exit_fast_tp_enabled') and held_days <= 7 and pnl_pct >= 0.15:
        alerts.append({
            "rule_name": "Fast Take Profit",
            "message": "🚀 [极速爆发] 短期爆发 (持仓<=7天, 收益>=15%)",
            "severity": "MEDIUM",
            "trigger_condition": f"持仓 {held_days}天 <= 7 AND 盈利 {pnl_pct*100:.1f}% >= 15%"
        })
        
    # 移动止盈: 最高 >= 30% AND 回撤 > 10%
    if is_enabled('is_exit_trailing_tp_enabled') and new_max_profit >= 0.30:
        drawdown = new_max_profit - pnl_pct
        if drawdown >= 0.10:
            alerts.append({
                "rule_name": "Trailing Stop",
                "message": f"📉 [利润回撤] 最高收益 {new_max_profit*100:.1f}%, 当前 {pnl_pct*100:.1f}%",
                "severity": "HIGH",
                "trigger_condition": f"回撤 {drawdown*100:.1f}% >= 10%"
            })

    # 技术止盈: QQQ RSI > 75 OR 突破布林上轨
    if is_enabled('is_exit_tech_tp_enabled'):
        rsi = qqq_indicators.get("rsi")
        bb_upper = qqq_indicators.get("bb_upper")
        last_price = qqq_indicators.get("last_price")
        
        technical_exit = False
        tech_msg = ""
        
        if rsi and rsi > 75:
            technical_exit = True
            tech_msg = f"RSI过热 ({rsi:.1f})"
        elif bb_upper and last_price and last_price > bb_upper:
            technical_exit = True
            tech_msg = "突破布林上轨"
            
        if technical_exit:
            alerts.append({
                "rule_name": "Technical Exit",
                "message": f"⚠️ [大盘过热] {tech_msg}",
                "severity": "MEDIUM",
                "trigger_condition": tech_msg
            })

    # 3. 风控规则
    
    # DTE 强制清仓
    if is_enabled('is_exit_dte_force_enabled') and dte < 90:
        alerts.append({
            "rule_name": "Force Exit (Time)",
            "message": "⛔ [强制清仓] (DTE < 90)",
            "severity": "CRITICAL",
            "trigger_condition": f"DTE {dte} < 90"
        })
    # DTE 移仓窗口
    elif is_enabled('is_exit_dte_warning_enabled') and dte < 120:
        alerts.append({
            "rule_name": "Rollover Window",
            "message": "⏳ [移仓窗口] (DTE < 120)",
            "severity": "MEDIUM",
            "trigger_condition": f"DTE {dte} < 120"
        })
        
    # 技术止损: QQQ 有效跌破 MA200 (< 99% of MA200)
    if is_enabled('is_exit_trend_stop_enabled'):
        ma200 = qqq_indicators.get("ma200")
        last_price = qqq_indicators.get("last_price")
        if ma200 and last_price and last_price < (ma200 * 0.99):
            alerts.append({
                "rule_name": "Trend Breakdown",
                "message": "🛑 [趋势崩坏] 有效跌破年线",
                "severity": "CRITICAL",
                "trigger_condition": f"价格 {last_price:.2f} < 0.99 * MA200 {ma200:.2f}"
            })

    # Formatting alerts
    for alert in alerts:
        alert["alert_type"] = "OPTION_SIGNAL"
        alert["position_id"] = position.id
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
