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
        
        # 基础信息
        rule_name = alert.get('rule_name', '')
        message = alert.get('message', '')
        trigger_condition = alert.get('trigger_condition', '')
        
        # === 恐慌加速度标签（仅 Level 2/3）===
        panic_data = alert.get("panic_acceleration")
        panic_label = ""
        panic_section = ""
        
        if panic_data and panic_data.get("is_panic"):
            panic_label = " 🧨 [恐慌加速度]"
        
        if panic_data:
            cond_a = panic_data.get("condition_a", (False, ""))
            cond_b = panic_data.get("condition_b", (False, ""))
            cond_c = panic_data.get("condition_c", (False, ""))
            conditions_met = panic_data.get("conditions_met", 0)
            
            panic_section = f"""
恐慌加速度检测（满足 {conditions_met}/3 条件）:
{"✅" if cond_a[0] else "❌"} 成交量: {cond_a[1]}
{"✅" if cond_b[0] else "❌"} 跌幅集中: {cond_b[1]}
{"✅" if cond_c[0] else "❌"} VIX暴涨: {cond_c[1]}
"""
        
        # === 动态 Delta 推荐 ===
        delta_rec = alert.get("delta_recommendation", {})
        
        if delta_rec.get("available"):
            vix_current = delta_rec.get("vix_current", 0)
            vix_ma20 = delta_rec.get("vix_ma20", 0)
            vix_ratio = delta_rec.get("vix_ratio", 0)
            iv_zone = delta_rec.get("iv_zone", "")
            delta_recommend = delta_rec.get("delta_recommend", "")
            explanation = delta_rec.get("explanation", "")
            
            delta_section = f"""VIX: {vix_current:.1f} (MA20={vix_ma20:.1f}, 比值={vix_ratio:.2f}) → {iv_zone}
Delta 推荐: {delta_recommend}
说明: {explanation}"""
        else:
            explanation = delta_rec.get("explanation", "VIX 数据不可用")
            delta_section = f"VIX: N/A → Delta 推荐: N/A ({explanation})"
        
        return f"""【QQQ 跌幅提醒】

规则: {rule_name}{panic_label}

{message}

触发条件: {trigger_condition}

当前价: ${current_price:.2f}

跌幅: {drop_pct:.2f}%
{panic_section}
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