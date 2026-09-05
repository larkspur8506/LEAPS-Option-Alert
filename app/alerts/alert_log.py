"""统一 AlertLog 写入层。

约定 (AlertLog 一致性要求):
- 企业微信实际发送的消息内容必须与 AlertLog 保存的 message 完全一致。
- 发送失败时仍记录完整 message, success=False。
- 不记录 secret / webhook URL (格式化文本本身不含敏感信息)。
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def log_alert(db, alert_type: str, rule_name: str, message: str, success: bool,
              error_message: Optional[str] = None) -> None:
    """
    将一条已格式化的完整消息写入 AlertLog。

    message 必须是企业微信实际发送的完整文本 (formatted_message),
    而不是摘要或 dict json。发送失败时 message 同样完整保存。
    """
    from app.database.models import AlertLog

    try:
        alert_log = AlertLog(
            alert_type=alert_type,
            rule_name=rule_name,
            message=message,
            sent_successfully=success,
            position_id=None,
        )
        if not success:
            alert_log.error_message = error_message or "Failed to send WeChat notification"
        db.add(alert_log)
        db.commit()
    except Exception as e:
        # AlertLog 写入失败只记录日志, 不影响业务状态与通知结果
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"[AlertLog] Failed to write alert log ({alert_type}): {e}")


def log_alert_legacy_json(db, alert: dict, success: bool) -> None:
    """兼容保留: 旧版 dict-json 包装写入 (scheduler cleanup 等历史路径)。

    新代码请使用 log_alert() (message=完整格式化文本)。
    """
    from app.database.models import AlertLog

    alert_log = AlertLog(
        alert_type=alert.get("alert_type", "SYSTEM_ALERT"),
        rule_name=alert.get("rule_name", ""),
        message=json.dumps(alert, default=str),
        sent_successfully=success,
        position_id=alert.get("position_id"),
    )
    if not success:
        alert_log.error_message = "Failed to send WeChat notification"
    db.add(alert_log)
    db.commit()
