import re
import requests
from typing import Dict
from datetime import datetime


def _redact_secrets(text: str) -> str:
    """移除日志中的 webhook key 等敏感参数, 防止 secret 泄漏到日志"""
    if not text:
        return text
    return re.sub(r"(key=)[^&\s'\"]+", r"\1***", text, flags=re.IGNORECASE)


class WeChatNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_ndx_grid_report(self, report_data: Dict) -> bool:
        message = self.format_ndx_grid_report(report_data)
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

    def format_ndx_grid_report(self, data: Dict) -> str:
        """
        NDX Grid Daily Report (Phase 6)。
        纯展示层: 只格式化 dashboard 数据, 不计算任何网格/信号逻辑。
        数据缺失/异常时渲染 N/A / NOT EVALUATED, 不误报。
        """
        ndx = data.get("dashboard", {}).get("ndx") or {}
        running = data.get("dashboard", {}).get("running_cycle")
        waiting = data.get("dashboard", {}).get("waiting_cycle")
        theo = data.get("dashboard", {}).get("theoretical_grid_position")
        latest = data.get("latest_cycle") or {}
        strategy = data.get("strategy", {})

        def price(v):
            return f"{v:,.2f}" if isinstance(v, (int, float)) else "N/A"

        # NDX Market (数据异常时不显示误导性数据)
        data_valid = bool(ndx.get("is_data_valid"))
        data_fresh = bool(ndx.get("is_data_fresh"))
        data_state = "FRESH" if (data_valid and data_fresh) else ("STALE" if data_valid else "UNAVAILABLE")
        signal = ndx.get("entry_signal")
        signal_text = {True: "YES", False: "NO"}.get(signal, "NOT EVALUATED")

        lines = [
            "NDX Grid Daily Report",
            data.get("date", ""),
            "",
            "NDX Market",
            f"Price: {price(ndx.get('last_price'))}",
            f"RSI14: {ndx.get('rsi'):.2f}" if isinstance(ndx.get("rsi"), (int, float)) else "RSI14: N/A",
            f"SMA200: {price(ndx.get('ma200'))}",
            f"1Y Ago: {price(ndx.get('price_1y_ago'))}",
            f"Data: {data_state}",
            "",
            "Entry Signal",
            signal_text,
            "",
            "Grid Strategy",
            f"Upper: +{strategy.get('upper_pct', 0.20) * 100:.0f}%",
            f"Lower: -{strategy.get('lower_pct', 0.20) * 100:.0f}%",
            f"Grid Count: {strategy.get('grid_count', 200)}",
            f"Leverage: {strategy.get('leverage', 5.0):.1f}x",
            f"Grid Step: N/A",
        ]

        # Grid Status (以 API status 为准, 无活动时用最近一条历史)
        if running:
            lines += [
                "",
                "Grid Status",
                "RUNNING",
                "",
                f"Base: {price(running.get('actual_base_price'))}",
                f"Upper: {price(running.get('actual_upper_price'))}",
                f"Lower: {price(running.get('actual_lower_price'))}",
                f"Grid Count: {running.get('actual_grid_count') if running.get('actual_grid_count') is not None else 'N/A'}",
                f"Leverage: {running.get('actual_leverage'):.1f}x" if isinstance(running.get("actual_leverage"), (int, float)) else "Leverage: N/A",
                f"Margin: {price(running.get('actual_margin'))}",
                f"Started: {running.get('started_at') or 'N/A'}",
            ]
            if theo:
                upper = running.get("actual_upper_price")
                lower = running.get("actual_lower_price")
                cur = ndx.get("last_price")
                lines += [
                    "",
                    "Theoretical Status",
                    f"Interval: {theo.get('grid_interval_index') if theo.get('grid_interval_index') is not None else 'N/A'} / {theo.get('grid_count') if theo.get('grid_count') is not None else 'N/A'}",
                    f"Position: {theo.get('position_ratio'):.2%}" if isinstance(theo.get("position_ratio"), (int, float)) else "Position: N/A",
                    f"Distance to Upper: {(upper - cur) / upper:.2%}" if isinstance(upper, (int, float)) and isinstance(cur, (int, float)) and upper else "Distance to Upper: N/A",
                    f"Distance to Lower: {(cur - lower) / lower:.2%}" if isinstance(lower, (int, float)) and isinstance(cur, (int, float)) and lower else "Distance to Lower: N/A",
                ]
            lines += [
                "",
                "⚠️ 理论状态仅用于提醒，不代表交易所实际持仓或实际盈亏。",
            ]
        elif waiting:
            lines += [
                "",
                "Grid Status",
                "WAITING — 等待用户确认并在交易所启动 Grid",
                "",
                f"Suggested Base: {price(waiting.get('suggested_base_price'))}",
                f"Suggested Upper: {price(waiting.get('suggested_upper_price'))}",
                f"Suggested Lower: {price(waiting.get('suggested_lower_price'))}",
                f"Grid Count: {waiting.get('suggested_grid_count') if waiting.get('suggested_grid_count') is not None else 'N/A'}",
                f"Leverage: {waiting.get('suggested_leverage'):.1f}x" if isinstance(waiting.get("suggested_leverage"), (int, float)) else "Leverage: N/A",
                f"Grid Step: {price(waiting.get('suggested_grid_step'))}",
                f"Created: {waiting.get('created_at') or 'N/A'}",
            ]
        elif latest:
            # 无活动 Grid: 用最近历史显示 CLOSED/STOPPED 及原因
            lines += [
                "",
                "Grid Status",
                latest.get("status", "NO ACTIVE GRID"),
            ]
            if latest.get("close_reason"):
                lines.append(f"Reason: {latest.get('close_reason')}")
        else:
            lines += [
                "",
                "Grid Status",
                "NO ACTIVE GRID",
            ]

        return "\n".join(lines)

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
            # 异常消息可能包含完整 webhook URL (含 key=SECRET), 脱敏后再输出
            print(f"[ERROR] Failed to send WeChat message: {_redact_secrets(str(e))}")
            return False


def get_wechat_notifier(webhook_url: str) -> WeChatNotifier:
    return WeChatNotifier(webhook_url)
