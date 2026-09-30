"""统一 AlertLog 写入层。

约定 (AlertLog 一致性要求):
- 企业微信实际发送的消息内容必须与 AlertLog 保存的 message 完全一致。
- 发送失败时仍记录完整 message, success=False。
- 不记录 secret / webhook URL (格式化文本本身不含敏感信息)。

时间戳约定 (P2 修复):
- `triggered_at` 一律写 **UTC naive**(`utcnow_naive()`), 与 SQLite
  `CURRENT_TIMESTAMP` 语义一致; 展示层再转美东时间。
  历史实现由 `server_default=func.now()` 写 UTC, 而查询用 ET aware datetime,
  导致"今日告警数"与保留期剪切点偏移 4~5 小时。
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def utcnow_naive() -> datetime:
    """当前 UTC 时间 (naive, 与 SQLite CURRENT_TIMESTAMP 同语义)。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log_alert(db, alert_type: str, rule_name: str, message: str, success: bool,
              error_message: Optional[str] = None, cycle_id: Optional[int] = None,
              position_id: Optional[int] = None,
              triggered_at: Optional[datetime] = None) -> None:
    """
    将一条已格式化的完整消息写入 AlertLog。

    message 必须是企业微信实际发送的完整文本 (formatted_message),
    而不是摘要或 dict json。发送失败时 message 同样完整保存。
    cycle_id 为历史 grid 遗留绑定列 (LEAPS 模式不写);
    position_id 把提醒与 OptionPosition 绑定, 支持重启后仍有效的落库级去重。
    """
    from app.database.models import AlertLog

    try:
        alert_log = AlertLog(
            alert_type=alert_type,
            rule_name=rule_name,
            message=message,
            sent_successfully=success,
            position_id=position_id,
            cycle_id=cycle_id,
            triggered_at=triggered_at or utcnow_naive(),
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


def count_alerts_today(db, alert_type: Optional[str] = None, now_utc: Optional[datetime] = None) -> int:
    """统计(UTC)当天已落库的告警条数; alert_type 为空则统计全部。"""
    from app.database.models import AlertLog

    now = now_utc or utcnow_naive()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    query = db.query(AlertLog).filter(AlertLog.triggered_at >= day_start)
    if alert_type:
        query = query.filter(AlertLog.alert_type == alert_type)
    try:
        return int(query.count())
    except Exception as e:
        logger.warning(f"[AlertLog] count_alerts_today failed ({alert_type}): {e}")
        return 0


def alerted_within(db, alert_type: str, within_seconds: Optional[int] = None,
                   cycle_id: Optional[int] = None,
                   position_id: Optional[int] = None,
                   now_utc: Optional[datetime] = None,
                   since_utc: Optional[datetime] = None) -> bool:
    """
    落库级去重查询: 指定时间窗内是否已存在同类型(且同 cycle/position)的告警记录。

    用于进程重启后仍然生效的去重 (内存态 dedup 在重启后会丢失):
    - 日报: within_seconds=86400, cycle_id=None -> 当天是否已发过
    - 加仓提醒等: 传 position_id, within_seconds 取足够大的窗口

    也可以通过 `since_utc` 直接给定窗起点 (例如"美东当天 00:00 对应的 UTC 时刻")。
    日报这类"按自然日"去重必须用 since_utc: 滚动 24 小时窗口会把"上一次日报"
    (恰好约 24 小时前) 误判为已发送, 导致隔天漏发。
    """
    from app.database.models import AlertLog

    now = now_utc or utcnow_naive()
    if since_utc is not None:
        since = since_utc
    else:
        since = now - timedelta(seconds=max(1, int(within_seconds or 0)))
    query = db.query(AlertLog).filter(
        AlertLog.alert_type == alert_type,
        AlertLog.triggered_at >= since,
    )
    if cycle_id is not None:
        query = query.filter(AlertLog.cycle_id == cycle_id)
    if position_id is not None:
        query = query.filter(AlertLog.position_id == position_id)
    try:
        return query.first() is not None
    except Exception as e:
        logger.warning(f"[AlertLog] alerted_within failed ({alert_type}): {e}")
        return False


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
        cycle_id=alert.get("cycle_id"),
        triggered_at=utcnow_naive(),
    )
    if not success:
        alert_log.error_message = "Failed to send WeChat notification"
    db.add(alert_log)
    db.commit()
