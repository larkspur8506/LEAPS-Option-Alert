"""QQQ LEAPS 监控引擎 (替代 grid_monitor 的角色)。

驱动 OptionPosition 生命周期:
- HOLDING: 刷新期权报价 (尽力而为) -> 评估退出线 (RSI_TP / TIME_STOP / DTE_FORCE)
  -> 命中即平仓 (HOLDING->CLOSED) 并通知; 未命中则评估加仓档位 -> 一次性提醒
- WAITING: TTL 超时 -> EXPIRED + 通知; 否则等待用户确认
- 无活跃仓位: 评估入场信号 -> 创建 WAITING + 通知

契约与 grid_monitor 一致:
- 数据无效/过期一律 SKIPPED (fail-closed), 不修改任何状态
- 通知/日志失败不影响已提交的状态变更
- 幂等: 状态迁移即终态, 不会重复触发
"""
import logging
from datetime import datetime, date
from typing import Dict, Any, Optional

import pandas as pd
from pytz import timezone
from sqlalchemy.orm import Session

from app.alerts.qqq_rules import check_entry_signal, evaluate_position, fetch_option_premium
from app.alerts import dedup
from app.alerts.alert_log import log_alert
from app.services import leaps_service
from app.notification.wechat import (
    WeChatNotifier,
    format_leaps_entry,
    format_leaps_waiting_expired,
    format_leaps_add_lot,
    format_leaps_exit,
)
from app.scheduler.trading_hours import get_latest_trading_day

logger = logging.getLogger(__name__)

et_tz = timezone("America/New_York")

DEFAULT_RSI_THRESHOLD = 35.0
DEFAULT_WAITING_TTL_TRADING_DAYS = 3
# 报价刷新最小间隔 (分钟): 期权链请求较重, 不跟随 5 分钟监控节奏
PREMIUM_REFRESH_MIN_MINUTES = 60


def _resolve(config: Optional[Any], getter: str, default: Any, cast=None):
    """运行时配置读取 + 非法回退 (与 grid_monitor 的 _resolve_* 同风格)。"""
    try:
        if config and hasattr(config, getter):
            value = getattr(config, getter)()
            if cast and not isinstance(value, cast[0] if isinstance(cast, tuple) else cast):
                raise ValueError(value)
            return value
    except Exception:
        pass
    return default


def _notify_with_log(db: Session, notifier: Optional[WeChatNotifier],
                     message: str, alert_type: str, rule_name: str,
                     position_id: Optional[int] = None) -> bool:
    """发送完整消息并写 AlertLog (message 与实发内容一致); 失败不抛出。"""
    if notifier is None:
        return False

    success = False
    try:
        success = bool(notifier.send_message(message))
    except Exception as e:
        logger.error(f"[LEAPS Monitor] Failed to send WeChat alert ({alert_type}): {e}")
        success = False

    try:
        log_alert(
            db,
            alert_type=alert_type,
            rule_name=rule_name,
            message=message,
            success=success,
            error_message="Failed to send WeChat notification" if not success else None,
            position_id=position_id,
        )
    except Exception as e:
        logger.warning(f"[LEAPS Monitor] Failed to write AlertLog ({alert_type}): {e}")

    return success


def _is_data_fresh(qqq_data: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """数据日期 >= 最近一个有效交易日 (NYSE 日历); 缺失/解析失败按不新鲜处理。"""
    data_timestamp_str = qqq_data.get("data_timestamp")
    if not data_timestamp_str:
        logger.warning("[LEAPS Freshness] data_timestamp missing from QQQ data.")
        return False

    try:
        data_date = pd.Timestamp(data_timestamp_str).date()
    except Exception as e:
        logger.warning(f"[LEAPS Freshness] Failed to parse data_timestamp '{data_timestamp_str}': {e}")
        return False

    if now is None:
        now = datetime.now(et_tz)
    if now.tzinfo is not None:
        now = now.astimezone(et_tz)
    today = now.date()

    try:
        latest_trading_date = get_latest_trading_day(now)
    except Exception as e:
        logger.error(f"[LEAPS Freshness] Failed to query trading calendar: {e}")
        return (today - data_date).days <= 3

    if latest_trading_date is None:
        logger.warning("[LEAPS Freshness] No trading day found in last 10 days. Treating as stale.")
        return False

    return data_date >= latest_trading_date


def _refresh_premium(db: Session, holding) -> None:
    """尽力刷新持仓期权权利金 (节流 60 分钟); 失败静默跳过, 不影响主流程。"""
    try:
        if holding.entry_price is None:
            return
        now = datetime.now(et_tz)
        last = holding.premium_updated_at
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone("UTC"))
            last = last.astimezone(et_tz)
            if (now - last).total_seconds() < PREMIUM_REFRESH_MIN_MINUTES * 60:
                return

        exp = holding.expiration_date
        if isinstance(exp, str):
            exp = date.fromisoformat(exp)
        if exp is None:
            return

        premium = fetch_option_premium(holding.strike, exp)
        if premium and premium > 0:
            holding.current_premium = float(premium)
            holding.premium_updated_at = datetime.now(timezone("UTC")).replace(tzinfo=None)
            entry = float(holding.entry_price)
            pnl = (premium / entry - 1.0) * 100.0 if entry > 0 else 0.0
            holding.max_pnl_pct = max(float(holding.max_pnl_pct or 0.0), pnl)
            db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug(f"[LEAPS Monitor] premium refresh skipped: {e}")


def _add_dedup_key(position_id: int, level_index: int) -> str:
    """加仓提醒去重 key: 绑定仓位与档位序号 (第 add_count 次加仓对应 levels[index])。"""
    return f"LEAPS_ADD_LOT_pos_{position_id}_lvl_{level_index}"


def _add_already_alerted(db: Session, position_id: int, next_count: int) -> bool:
    """落库级去重查询 (进程重启后仍生效): 该仓位是否已发过"加到 next_count 张"的提醒。"""
    from app.database.models import AlertLog
    try:
        marker = f"已达到第 {next_count} 张"
        row = (
            db.query(AlertLog)
            .filter(
                AlertLog.alert_type == "LEAPS_ADD_LOT",
                AlertLog.position_id == position_id,
            )
            .order_by(AlertLog.id.desc())
            .limit(20)
            .all()
        )
        return any(marker in (r.message or "") for r in row)
    except Exception as e:
        logger.warning(f"[LEAPS Monitor] add-alert dedup query failed: {e}")
        return False


def process_leaps_position(
    db: Session,
    qqq_data: Dict[str, Any],
    notifier: Optional[WeChatNotifier] = None,
    config: Optional[Any] = None,
) -> Dict[str, Any]:
    """驱动 LEAPS 仓位状态机 (每 5 分钟由 scheduler 调用)。"""
    if not qqq_data or not qqq_data.get("is_data_valid", True) or not qqq_data.get("last_price"):
        logger.warning("[LEAPS Monitor] No valid QQQ market data provided, skipping check.")
        return {"status": "SKIPPED", "reason": "NO_VALID_DATA"}

    try:
        current_price = float(qqq_data["last_price"])
    except (ValueError, TypeError):
        logger.warning(f"[LEAPS Monitor] Invalid last_price value: {qqq_data.get('last_price')}, skipping.")
        return {"status": "SKIPPED", "reason": "INVALID_PRICE"}

    if current_price <= 0:
        logger.warning(f"[LEAPS Monitor] Non-positive price: {current_price}, skipping.")
        return {"status": "SKIPPED", "reason": "NON_POSITIVE_PRICE"}

    if not _is_data_fresh(qqq_data):
        logger.warning(
            f"[LEAPS Monitor] QQQ data is stale (data_timestamp={qqq_data.get('data_timestamp')}). "
            f"Skipping all monitoring and entry signal checks. "
            f"Existing HOLDING/WAITING positions are NOT modified."
        )
        return {"status": "SKIPPED", "reason": "STALE_DATA"}

    # -------------------------------------------------------------
    # 1. HOLDING: 退出线 / 加仓判定
    # -------------------------------------------------------------
    holding = leaps_service.get_holding_position(db)
    if holding:
        _refresh_premium(db, holding)
        evaluation = evaluate_position(holding, qqq_data, config)

        breach = evaluation.get("breach")
        if breach:
            btype = breach["type"]
            rec = leaps_service.close_position(db, holding.id, reason=btype)
            logger.info(f"[LEAPS Monitor] Position #{rec.id} closed: {btype}")
            message = format_leaps_exit(rec, breach, qqq_data)
            _notify_with_log(db, notifier, message, f"LEAPS_{btype}", f"LEAPS {btype}", position_id=rec.id)
            return {"status": "OK", "action": "CLOSED", "reason": btype, "position_id": rec.id}

        add_trigger = evaluation.get("add_trigger")
        if add_trigger:
            next_count = int(add_trigger.get("next_count") or 0)
            if not _add_already_alerted(db, holding.id, next_count) and \
                    dedup.should_alert(_add_dedup_key(holding.id, next_count)):
                message = format_leaps_add_lot(holding, add_trigger, qqq_data)
                _notify_with_log(db, notifier, message, "LEAPS_ADD_LOT", "LEAPS 加仓提醒",
                                 position_id=holding.id)
                return {"status": "OK", "action": "ADD_ALERTED", "position_id": holding.id}

        return {"status": "OK", "action": "HOLDING_MONITORED", "position_id": holding.id}

    # -------------------------------------------------------------
    # 2. WAITING: TTL 超时 -> EXPIRED
    # -------------------------------------------------------------
    waiting = leaps_service.get_waiting_position(db)
    if waiting:
        ttl_days = _resolve(config, "get_waiting_ttl_trading_days", DEFAULT_WAITING_TTL_TRADING_DAYS)
        ttl_state = leaps_service.waiting_ttl_state(waiting, ttl_days)
        if ttl_state.get("expired"):
            rec = leaps_service.expire_waiting(db, waiting.id)
            logger.info(f"[LEAPS Monitor] WAITING #{rec.id} expired (TTL={ttl_days} trading days)")
            message = format_leaps_waiting_expired(rec, ttl_state)
            _notify_with_log(db, notifier, message, "LEAPS_WAITING_EXPIRED", "LEAPS 建议过期",
                             position_id=rec.id)
            return {"status": "OK", "action": "EXPIRED", "position_id": rec.id}
        return {"status": "OK", "action": "WAITING_QUEUED", "position_id": waiting.id}

    # -------------------------------------------------------------
    # 3. 无活跃仓位: 入场信号判定 (收盘确认制)
    # -------------------------------------------------------------
    entry_rsi = _resolve(config, "get_entry_rsi_threshold", DEFAULT_RSI_THRESHOLD)
    signal = check_entry_signal(qqq_data, rsi_threshold=entry_rsi)
    if not signal:
        return {"status": "OK", "action": "NO_SIGNAL"}

    rec = leaps_service.create_waiting_position(db, signal, config)
    logger.info(f"[LEAPS Monitor] Entry signal -> WAITING #{rec.id} "
                f"(base={rec.signal_base_price}, rsi={rec.signal_rsi})")
    message = format_leaps_entry(rec, qqq_data, config)
    _notify_with_log(db, notifier, message, "LEAPS_ENTRY", "LEAPS 入场信号", position_id=rec.id)
    return {"status": "OK", "action": "ENTRY_SIGNAL", "position_id": rec.id}
