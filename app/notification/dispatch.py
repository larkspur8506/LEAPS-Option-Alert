"""多通道通知分发 (2026-10-11 评审 P2-7)。

组合企业微信 + Telegram 两个通道:
- **任一通道送达即视为成功** -> 去重/重试语义与通道数无关, 不会因一个通道挂掉
  而反复重发到另一个通道;
- 全部失败才返回 False (调用方据此保留重试机会);
- 部分送达会打 warning 日志, 便于排障。

未配置任何通道时返回 None, 与历史 `get_wechat_notifier("")` 的语义一致。
"""
import logging
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class CompositeNotifier:
    """把同一条消息分发到多个通道 (通道需实现 send_message(str) -> bool)。"""

    def __init__(self, channels: List[Tuple[str, Any]]):
        self.channels = [(name, ch) for name, ch in (channels or []) if ch is not None]

    def __bool__(self) -> bool:
        return bool(self.channels)

    def send_message(self, message: str) -> bool:
        if not self.channels:
            return False

        results: List[Tuple[str, bool]] = []
        for name, channel in self.channels:
            try:
                ok = bool(channel.send_message(message))
            except Exception as e:
                logger.error("[Notify] channel %s raised: %s", name, e)
                ok = False
            results.append((name, ok))

        delivered = any(ok for _, ok in results)
        if not delivered:
            logger.error("[Notify] all channels failed: %s", results)
        elif not all(ok for _, ok in results):
            logger.warning("[Notify] partial delivery: %s", results)
        return delivered

    # ---- 日报便捷封装 (与企业微信通道保持同一接口) ----
    def format_leaps_report(self, report_data: dict) -> str:
        from app.notification.wechat import format_leaps_daily_report

        return format_leaps_daily_report(report_data)

    def send_leaps_report(self, report_data: dict) -> bool:
        return self.send_message(self.format_leaps_report(report_data))


def get_notifier(config: Any = None) -> Optional[CompositeNotifier]:
    """按运行时配置构造多通道 notifier; 一个通道都没配则返回 None。"""
    channels: List[Tuple[str, Any]] = []

    webhook = ""
    if config is not None and hasattr(config, "get_wechat_webhook_url"):
        try:
            webhook = (config.get_wechat_webhook_url() or "").strip()
        except Exception:
            webhook = ""
    if webhook:
        from app.notification.wechat import get_wechat_notifier

        channels.append(("wecom", get_wechat_notifier(webhook)))

    token = chat_id = ""
    if config is not None and hasattr(config, "get_telegram_bot_token"):
        try:
            token = (config.get_telegram_bot_token() or "").strip()
            chat_id = (config.get_telegram_chat_id() or "").strip()
        except Exception:
            token = chat_id = ""
    if token and chat_id:
        from app.notification.telegram import TelegramNotifier

        channels.append(("telegram", TelegramNotifier(token, chat_id)))

    if not channels:
        return None
    return CompositeNotifier(channels)
