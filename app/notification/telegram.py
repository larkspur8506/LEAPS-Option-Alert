"""Telegram 通知通道 (第二通道, 可选)。

2026-10-11 评审 P2-7: 原先只有企业微信单通道; 用户主通道是 Telegram。
配置来源: 环境变量 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, 任一为空即禁用该通道。

设计: 与企业微信通道同契约 (`send_message(message) -> bool`), 由
`app.notification.dispatch.CompositeNotifier` 统一分发; 单通道失败不影响另一通道。
"""
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
# Telegram 单条消息上限 4096 字符; 按字节留余量 (中文 3 字节/字) 后分段
MAX_CHUNK_BYTES = 3800
SEND_ATTEMPTS = 3
SEND_BACKOFF_SECONDS = (1.0, 3.0)


def _redact(text: str, token: str = "") -> str:
    """去掉日志里的 bot token (异常信息里可能带完整 URL)。"""
    out = str(text or "")
    if token:
        out = out.replace(token, "***")
    return out


class TelegramNotifier:
    """把已格式化的完整消息发到 Telegram (与企业微信通道同契约)。"""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send_message(self, message: str) -> bool:
        if not self.enabled:
            return False

        from app.notification.wechat import split_message_chunks

        chunks = split_message_chunks(message or "", MAX_CHUNK_BYTES)
        if len(chunks) > 1:
            logger.info("[Telegram] long message (%d bytes) sent in %d chunks",
                        len((message or "").encode("utf-8")), len(chunks))
        for chunk in chunks:
            if not self._post_chunk(chunk):
                return False
        return True

    def _post_chunk(self, chunk: str) -> bool:
        url = TELEGRAM_API_URL.format(token=self.bot_token)
        payload = {
            "chat_id": self.chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        last_err = "unknown"
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except Exception:
                        data = {}
                    if data.get("ok"):
                        return True
                    last_err = f"API {data.get('description', '')}"
                else:
                    last_err = f"HTTP {response.status_code}"
            except Exception as e:
                last_err = _redact(str(e), self.bot_token)

            if attempt < SEND_ATTEMPTS:
                time.sleep(SEND_BACKOFF_SECONDS[min(attempt - 1, len(SEND_BACKOFF_SECONDS) - 1)])

        logger.error("[Telegram] send failed after %d attempts: %s", SEND_ATTEMPTS, last_err)
        return False
