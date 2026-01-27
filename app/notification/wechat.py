import json
import requests
from typing import Dict, Optional
from datetime import datetime


class WeChatNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_qqq_alert(self, alert: Dict) -> bool:
        message = self._format_qqq_alert(alert)
        return self._send_message(message)

    def send_option_alert(self, alert: Dict, position_ticker: str) -> bool:
        message = self._format_option_alert(alert, position_ticker)
        return self._send_message(message)

    def _format_qqq_alert(self, alert: Dict) -> str:
        timestamp = alert.get("timestamp", datetime.now())
        time_str = timestamp.strftime("%Y-%m-%d %H:%M:%S") if isinstance(timestamp, datetime) else str(timestamp)
        drop_pct = alert.get("drop_percent", 0)
        current_price = alert.get("trigger_price", alert.get("current_price", 0))
        
        # VIX 指数信息
        vix_index = alert.get("vix_index")
        vix_status = alert.get("vix_status", "未知")
        
        if vix_index is not None:
            vix_display = f"VIX指数: {vix_index:.2f} ({vix_status})"
        else:
            vix_display = f"VIX指数: 未知 ({vix_status})"
        
        return f"""【QQQ 跌幅提醒】

规则: {alert.get('rule_name')} - {alert.get('message', '')}

触发条件: {alert.get('trigger_condition', '')}

当前价: ${current_price:.2f}

跌幅: {drop_pct:.2f}%

{vix_display}

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

        return f"""【期权止盈提醒】

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

        return f"""【期权时间风险提醒】

标的: {position_ticker}

距离到期: {dte} 天

到期日: {expiration_date}

请注意期权的时间价值衰减风险"""

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
            print(f"[ERROR] Failed to send WeChat message: {e}")
            return False


def get_wechat_notifier(webhook_url: str) -> WeChatNotifier:
    return WeChatNotifier(webhook_url)