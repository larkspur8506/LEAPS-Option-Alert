import json
import re
import requests
from typing import Dict, Optional
from datetime import datetime


def _redact_secrets(text: str) -> str:
    """移除日志中的 webhook key 等敏感参数, 防止 secret 泄漏到日志"""
    if not text:
        return text
    return re.sub(r"(key=)[^&\s'\"]+", r"\1***", text, flags=re.IGNORECASE)


class WeChatNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_qqq_alert(self, alert: Dict) -> bool:
        message = self._format_qqq_alert(alert)
        return self._send_message(message)

    def send_option_alert(self, alert: Dict, position_ticker: str) -> bool:
        message = self._format_option_alert(alert, position_ticker)
        return self._send_message(message)

    def send_daily_report(self, report_data: Dict) -> bool:
        message = self._format_daily_report(report_data)
        return self._send_message(message)

    def send_ndx_entry_alert(self, cycle, current_price: float) -> bool:
        message = self._format_ndx_entry_alert(cycle, current_price)
        return self._send_message(message)

    def send_ndx_upper_alert(self, cycle, current_price: float) -> bool:
        message = self._format_ndx_upper_alert(cycle, current_price)
        return self._send_message(message)

    def send_ndx_lower_alert(self, cycle, current_price: float) -> bool:
        message = self._format_ndx_lower_alert(cycle, current_price)
        return self._send_message(message)

    def _format_daily_report(self, data: Dict) -> str:
        date_str = data.get("date", datetime.now().strftime("%Y-%m-%d"))
        qqq_price = data.get("qqq_price", 0.0)
        sma200 = data.get("sma200", 0.0)
        above_below = "之上" if qqq_price > sma200 else "之下"
        consec_days = data.get("consecutive_days", 0)
        
        price_1y = data.get("price_1y", 0.0)
        pct_change = ((qqq_price - price_1y) / price_1y * 100) if price_1y > 0 else 0.0
        pct_sign = "+" if pct_change >= 0 else ""
        
        rsi = data.get("rsi", 0.0)
        
        entry_met = data.get("entry_met", False)
        entry_status = "满足" if entry_met else "不满足"
        unmet_cond = data.get("unmet_conditions", "")
        unmet_str = f"（未满足条件：{unmet_cond}）" if not entry_met and unmet_cond else ""
        
        current_pos = data.get("current_positions", 0)
        max_pos = data.get("max_positions", "N")
        
        stop_warn = data.get("stop_warning", False)
        stop_status = "警告" if stop_warn else "正常"
        stop_extra = f"（均线跌破第{consec_days}日）" if stop_warn else ""

        return f"""📅 日期：{date_str}

💹 QQQ收盘价：${qqq_price:.2f}

📊 SMA200：${sma200:.2f}（当前价格在均线{above_below}，连续{consec_days}日）

📈 一年前收盘价：${price_1y:.2f}（当前较一年前 {pct_sign}{pct_change:.1f}%）

📉 RSI(14)：{rsi:.1f}

🎯 入场信号：{entry_status}{unmet_str}

📋 当前持仓：{current_pos}张（上限{max_pos}张）

⚠️ 止损状态：{stop_status}{stop_extra}"""

    def _format_qqq_alert(self, alert: Dict) -> str:
        timestamp = alert.get("timestamp", datetime.now())
        time_str = timestamp.strftime("%Y-%m-%d %H:%M:%S") if isinstance(timestamp, datetime) else str(timestamp)
        current_price = alert.get("trigger_price", alert.get("current_price", 0))
        
        # 基础信息
        rule_name = alert.get('rule_name', '')
        message = alert.get('message', '')
        trigger_condition = alert.get('trigger_condition', '')
        
        # 期权建仓指引
        delta_rec = alert.get("delta_recommendation", {})
        delta_section = ""
        if delta_rec.get("available"):
            delta_recommend = delta_rec.get("delta_recommend", "")
            expiration = delta_rec.get("expiration", "")
            explanation = delta_rec.get("explanation", "")
            
            delta_section = f"""
【期权建仓指引】
到期日要求: {expiration}
Delta 要求: {delta_recommend}
策略说明: {explanation}"""

        return f"""【QQQ 长期复利引擎 - 入场信号】

规则: {rule_name}

{message}

触发条件: {trigger_condition}

QQQ 当前价: ${current_price:.2f}
{delta_section}

时间: {time_str}"""

    def _format_option_alert(self, alert: Dict, position_ticker: str) -> str:
        alert_type = alert.get("alert_type", "")

        if alert_type == "OPTION_MAX_HOLDING":
            return self._format_max_holding_alert(alert, position_ticker)
        elif alert_type == "OPTION_TAKE_PROFIT":
            return self._format_take_profit_alert(alert, position_ticker)
        elif alert_type == "OPTION_STOP_LOSS":
            return self._format_stop_loss_alert(alert, position_ticker)
        elif alert_type == "OPTION_TIME":
            return self._format_dte_alert(alert, position_ticker)
        else:
            return f"【期权提醒】\n\n{alert.get('message', '')}"

    def _format_max_holding_alert(self, alert: Dict, position_ticker: str) -> str:
        return f"""【期权最大持仓周期提醒】

标的: {position_ticker}

消息: {alert.get('message', '')}

当前价: ${alert.get('current_price', 0):.2f}

持仓天数: {alert.get('days_held', 0)} 天

最大周期: {alert.get('max_days', 0)} 天"""

    def _format_take_profit_alert(self, alert: Dict, position_ticker: str) -> str:
        profit_pct = alert.get('profit_pct', 0)
        days_held = alert.get('days_held', 0)
        rule_name = alert.get('rule_name', '')
        entry_price = alert.get('entry_price', 0)
        current_price = alert.get('current_price', 0)

        return f"""【期权阶梯止盈提醒】

标的: {position_ticker}

规则: {rule_name}

入场价: ${entry_price:.2f}

当前价: ${current_price:.2f}

盈利: +{profit_pct:.1f}%

持仓天数: {days_held} 天"""

    def _format_stop_loss_alert(self, alert: Dict, position_ticker: str) -> str:
        loss_pct = alert.get('loss_pct', 0)
        entry_price = alert.get('entry_price', 0)
        current_price = alert.get('current_price', 0)

        return f"""【期权止损提醒】

标的: {position_ticker}

入场价: ${entry_price:.2f}

当前价: ${current_price:.2f}

亏损: {loss_pct:.1f}%"""

    def _format_dte_alert(self, alert: Dict, position_ticker: str) -> str:
        dte = alert.get('dte', 0)
        expiration_date = alert.get('expiration_date', '')

        return f"""【期权强制平仓提醒】

标的: {position_ticker}

距离到期: {dte} 天 (<=180天)

到期日: {expiration_date}

触发红线风控：请立即平仓以规避期权末期加速的时间价值衰减（Theta Decay）！"""

    def _format_ndx_entry_alert(self, cycle, current_price: float) -> str:
        base = getattr(cycle, "suggested_base_price", None) if not isinstance(cycle, dict) else cycle.get("suggested_base_price")
        upper = getattr(cycle, "suggested_upper_price", None) if not isinstance(cycle, dict) else cycle.get("suggested_upper_price")
        lower = getattr(cycle, "suggested_lower_price", None) if not isinstance(cycle, dict) else cycle.get("suggested_lower_price")
        count = getattr(cycle, "suggested_grid_count", None) if not isinstance(cycle, dict) else cycle.get("suggested_grid_count")
        leverage = getattr(cycle, "suggested_leverage", None) if not isinstance(cycle, dict) else cycle.get("suggested_leverage")

        base_val = f"{base:.2f}" if base is not None else "N/A"
        upper_val = f"{upper:.2f}" if upper is not None else "N/A"
        lower_val = f"{lower:.2f}" if lower is not None else "N/A"
        count_val = count if count is not None else 200
        leverage_val = f"{int(leverage)}x" if leverage is not None else "5x"

        return f"""NDX 开仓信号

当前价格：{current_price:.2f}

建议网格：
下限：{lower_val}
基准：{base_val}
上限：{upper_val}

网格数量：{count_val}
杠杆：{leverage_val}

状态：WAITING

请手动在交易所创建网格后，再到后台确认实际参数。"""

    def _format_ndx_upper_alert(self, cycle, current_price: float) -> str:
        cycle_id = getattr(cycle, "id", None) if not isinstance(cycle, dict) else cycle.get("id")
        actual_upper = getattr(cycle, "actual_upper_price", None) if not isinstance(cycle, dict) else cycle.get("actual_upper_price")
        upper_val = f"{actual_upper:.2f}" if actual_upper is not None else "N/A"

        return f"""NDX 网格上限触发

当前价格：{current_price:.2f}
实际上限：{upper_val}

GridCycle ID：{cycle_id}

状态：CLOSED
原因：UPPER_REACHED"""

    def _format_ndx_lower_alert(self, cycle, current_price: float) -> str:
        cycle_id = getattr(cycle, "id", None) if not isinstance(cycle, dict) else cycle.get("id")
        actual_lower = getattr(cycle, "actual_lower_price", None) if not isinstance(cycle, dict) else cycle.get("actual_lower_price")
        lower_val = f"{actual_lower:.2f}" if actual_lower is not None else "N/A"

        return f"""NDX 网格下限触发

当前价格：{current_price:.2f}
实际下限：{lower_val}

GridCycle ID：{cycle_id}

状态：STOPPED
原因：LOWER_BREACHED"""

    def _send_message(self, message: str) -> bool:
        if not self.webhook_url:
            print(f"[WARN] WeChat webhook URL not configured, skipping alert: {message[:100]}")
            return False

        try:
            payload = {
                "msgtype": "text",
                "text": {
                    "content": message,
                    "mentioned_list": []
                }
            }
            
            response = requests.post(
                self.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("errcode") == 0:
                    print(f"[INFO] WeChat alert sent successfully")
                    return True
                else:
                    print(f"[ERROR] WeChat API error: {result}")
                    return False
            else:
                print(f"[ERROR] WeChat HTTP error: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            return False


def get_wechat_notifier(webhook_url: str) -> WeChatNotifier:
    return WeChatNotifier(webhook_url)