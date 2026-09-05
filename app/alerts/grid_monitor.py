"""
NDX Grid Monitor and Evaluation Engine.
Drives the GridCycle lifecycle based on incoming NDX market data:
- RUNNING cycle: monitors upper/lower boundaries (>= actual_upper -> CLOSED, < actual_lower -> STOPPED)
- No RUNNING and no WAITING: evaluates entry signals -> creates WAITING
- Idempotent: once CLOSED/STOPPED or WAITING, avoids duplicate triggers/notifications
- Resilient: WeChat notification failures do not roll back state transitions
- Data freshness: skips all state changes when market data is stale
"""
import logging
from datetime import datetime
from typing import Dict, Any, Optional

import pandas as pd
from pytz import timezone
from sqlalchemy.orm import Session

from app.alerts.grid_cycle import (
    get_running_grid_cycle,
    get_waiting_grid_cycle,
    create_waiting_grid_cycle,
    close_grid_cycle,
    stop_grid_cycle
)
from app.alerts.ndx_rules import check_ndx_entry_signals
from app.alerts import dedup
from app.alerts.alert_log import log_alert
from app.notification.wechat import (
    WeChatNotifier,
    format_ndx_grid_entry,
    format_ndx_grid_proximity,
    format_ndx_grid_closed,
    format_ndx_grid_stopped,
)
from app.scheduler.trading_hours import get_latest_trading_day

logger = logging.getLogger(__name__)

et_tz = timezone("America/New_York")

# 接近入场阈值 (proximity) 规则: threshold < RSI <= threshold + PROXIMITY_DISTANCE
PROXIMITY_DISTANCE = 5.0
DEFAULT_RSI_THRESHOLD = 35.0


def _resolve_rsi_threshold(config: Optional[Any]) -> float:
    """从运行时配置读取 RSI 入场阈值; 缺失/非法时回退默认 35.0"""
    try:
        if config and hasattr(config, "get_rsi_threshold"):
            value = config.get_rsi_threshold()
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return float(value)
    except Exception:
        pass
    return DEFAULT_RSI_THRESHOLD


def _notify_with_log(db: Session, notifier: Optional[WeChatNotifier],
                     message: str, alert_type: str, rule_name: str) -> bool:
    """
    发送已格式化的完整消息并写入 AlertLog (message 与实际发送内容完全一致)。
    notifier 为 None (未配置 webhook) 时跳过, 与既有行为一致。
    通知/日志失败均不影响已提交的状态变更。
    """
    if notifier is None:
        return False

    success = False
    try:
        success = bool(notifier.send_message(message))
    except Exception as e:
        logger.error(f"[NDX Monitor] Failed to send WeChat alert ({alert_type}): {e}")
        success = False

    try:
        log_alert(
            db,
            alert_type=alert_type,
            rule_name=rule_name,
            message=message,
            success=success,
            error_message="Failed to send WeChat notification" if not success else None,
        )
    except Exception as e:
        logger.warning(f"[NDX Monitor] Failed to write AlertLog ({alert_type}): {e}")

    return success


def _is_data_fresh(ndx_data: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """
    判断 NDX 数据是否属于最近一个有效交易日（即足够新鲜可用于决策）。

    判断逻辑:
    1. 解析 ndx_data["data_timestamp"]（yfinance 返回的最后一条K线日期）
    2. 使用 app/scheduler/trading_hours.py 的 NYSE 交易日历确定
       当前时间对应的最近一个有效交易日（自动兼容周末/美股节假日）
    3. 如果数据日期 >= 最近交易日，则认为新鲜
    4. 若 data_timestamp 缺失或解析失败，返回 False（安全第一）

    不强制 5 分钟级别的新鲜度，因为 yfinance 日K数据不是 tick-level。
    只要数据日期不落后于最近一个有效交易日即可。
    """
    data_timestamp_str = ndx_data.get("data_timestamp")
    if not data_timestamp_str:
        logger.warning("[NDX Freshness] data_timestamp missing from NDX data.")
        return False

    try:
        # data_timestamp 格式示例: "2026-09-03 00:00:00-04:00" 或 "2026-09-03"
        data_ts = pd.Timestamp(data_timestamp_str)
        data_date = data_ts.date()
    except Exception as e:
        logger.warning(f"[NDX Freshness] Failed to parse data_timestamp '{data_timestamp_str}': {e}")
        return False

    if now is None:
        now = datetime.now(et_tz)
    if now.tzinfo is not None:
        now = now.astimezone(et_tz)
    today = now.date()

    # 复用 trading_hours 的 NYSE 日历: 周末/节假日自动回溯到最近交易日
    try:
        latest_trading_date = get_latest_trading_day(now)
    except Exception as e:
        logger.error(f"[NDX Freshness] Failed to query trading calendar: {e}")
        # 日历查询失败时退化为保守回退：数据日期不能比今天早超过 3 天
        return (today - data_date).days <= 3

    if latest_trading_date is None:
        logger.warning("[NDX Freshness] No trading day found in last 10 days. Treating as stale.")
        return False

    is_fresh = data_date >= latest_trading_date
    if not is_fresh:
        logger.warning(
            f"[NDX Freshness] Stale data detected: data_date={data_date}, "
            f"latest_trading_date={latest_trading_date}, today={today}. Skipping."
        )
    else:
        logger.debug(
            f"[NDX Freshness] Data is fresh: data_date={data_date}, "
            f"latest_trading_date={latest_trading_date}."
        )

    return is_fresh


def process_ndx_grid_cycle(
    db: Session,
    ndx_data: Dict[str, Any],
    notifier: Optional[WeChatNotifier] = None,
    config: Optional[Any] = None
) -> Dict[str, Any]:
    """
    驱动 NDX 合约网格监控状态机。

    执行顺序:
    1. 校验 NDX 数据的有效性
    2. 查询是否存在 RUNNING 周期:
       - 若存在，检查 upper (>= actual_upper) 和 lower (< actual_lower) 边界
       - 触发后修改数据库状态 (CLOSED / STOPPED) 并发送微信通知，直接结束
    3. 若无 RUNNING，查询是否存在 WAITING 周期:
       - 若存在，不检查新信号，直接结束
    4. 若既无 RUNNING 也无 WAITING:
       - 计算 NDX 开仓信号
       - 信号成立则创建 WAITING 周期并发送建议通知
    """
    if not ndx_data or not ndx_data.get("is_data_valid", True) or not ndx_data.get("last_price"):
        logger.warning("[NDX Monitor] No valid NDX market data provided, skipping check.")
        return {"status": "SKIPPED", "reason": "NO_VALID_DATA"}

    try:
        current_price = float(ndx_data["last_price"])
    except (ValueError, TypeError):
        logger.warning(f"[NDX Monitor] Invalid last_price value: {ndx_data.get('last_price')}, skipping.")
        return {"status": "SKIPPED", "reason": "INVALID_PRICE"}

    if current_price <= 0:
        logger.warning(f"[NDX Monitor] Non-positive price: {current_price}, skipping.")
        return {"status": "SKIPPED", "reason": "NON_POSITIVE_PRICE"}

    # -------------------------------------------------------------
    # 0. 数据新鲜度检查
    #    - 确保数据属于最近一个交易日，防止因 yfinance 缓存/网络故障
    #      使用陈旧数据误触发 Entry Signal 或错误关闭 RUNNING 周期
    #    - 若数据陈旧，所有操作跳过，RUNNING 保持不变
    # -------------------------------------------------------------
    if not _is_data_fresh(ndx_data):
        logger.warning(
            f"[NDX Monitor] NDX data is stale (data_timestamp={ndx_data.get('data_timestamp')}). "
            f"Skipping all monitoring and entry signal checks. "
            f"Existing RUNNING/WAITING cycles are NOT modified."
        )
        return {"status": "SKIPPED", "reason": "STALE_DATA"}
    # -------------------------------------------------------------
    # 1. 查询 RUNNING 周期
    # -------------------------------------------------------------
    running_cycle = get_running_grid_cycle(db)
    if running_cycle:
        upper = running_cycle.actual_upper_price
        lower = running_cycle.actual_lower_price

        # 上限触发: current_price >= actual_upper_price
        if upper is not None and current_price >= upper:
            logger.info(
                f"[NDX Monitor] Upper boundary reached: price={current_price:.2f} >= actual_upper={upper:.2f}. "
                f"Closing cycle ID={running_cycle.id} with UPPER_REACHED."
            )
            # 状态变更优先于通知并提交事务
            closed_cycle = close_grid_cycle(db, running_cycle.id, reason="UPPER_REACHED")

            # 发送微信通知 (异常隔离，不影响已持久化的状态)
            # message = 实际发送内容, 同时完整写入 AlertLog
            if notifier:
                try:
                    message = format_ndx_grid_closed(closed_cycle, current_price, indicators=ndx_data)
                    _notify_with_log(db, notifier, message, "NDX_GRID_CLOSED", "NDX Grid Upper Reached")
                except Exception as e:
                    logger.error(f"[NDX Monitor] Failed to send WeChat upper alert for cycle {closed_cycle.id}: {e}")

            # cycle 结束: 允许下一个 cycle 重新触发 proximity 提醒
            dedup.clear_proximity()

            return {"status": "CLOSED", "reason": "UPPER_REACHED", "cycle_id": closed_cycle.id}

        # 下限触发: current_price < actual_lower_price (严格小于)
        elif lower is not None and current_price < lower:
            logger.info(
                f"[NDX Monitor] Lower boundary breached: price={current_price:.2f} < actual_lower={lower:.2f}. "
                f"Stopping cycle ID={running_cycle.id} with LOWER_BREACHED."
            )
            # 状态变更优先于通知并提交事务
            stopped_cycle = stop_grid_cycle(db, running_cycle.id, reason="LOWER_BREACHED")

            # 发送微信通知 (异常隔离，不影响已持久化的状态)
            if notifier:
                try:
                    message = format_ndx_grid_stopped(stopped_cycle, current_price, indicators=ndx_data)
                    _notify_with_log(db, notifier, message, "NDX_GRID_STOPPED", "NDX Grid Lower Breached")
                except Exception as e:
                    logger.error(f"[NDX Monitor] Failed to send WeChat lower alert for cycle {stopped_cycle.id}: {e}")

            # cycle 结束: 允许下一个 cycle 重新触发 proximity 提醒
            dedup.clear_proximity()

            return {"status": "STOPPED", "reason": "LOWER_BREACHED", "cycle_id": stopped_cycle.id}

        else:
            logger.debug(
                f"[NDX Monitor] RUNNING cycle ID={running_cycle.id} in normal range "
                f"[{lower:.2f}, {upper:.2f}]. Price={current_price:.2f}."
            )
            return {"status": "RUNNING_NO_CHANGE", "cycle_id": running_cycle.id}

    # -------------------------------------------------------------
    # 2. 查询 WAITING 周期
    # -------------------------------------------------------------
    waiting_cycle = get_waiting_grid_cycle(db)
    if waiting_cycle:
        logger.debug(
            f"[NDX Monitor] WAITING cycle ID={waiting_cycle.id} already exists. "
            f"Skipping entry signal check."
        )
        return {"status": "WAITING_EXISTS", "cycle_id": waiting_cycle.id}

    # -------------------------------------------------------------
    # 3. 既无 RUNNING 也无 WAITING: 检查 NDX 开仓信号
    # -------------------------------------------------------------
    alerts = check_ndx_entry_signals(current_price, ndx_data, config=config)
    if alerts:
        suggested_grid = alerts[0].get("suggested_grid")
        logger.info(
            f"[NDX Monitor] Entry signal triggered for NDX at price={current_price:.2f}. "
            f"Creating WAITING cycle."
        )
        new_cycle = create_waiting_grid_cycle(db, suggested_grid)

        # 发送微信开仓建议通知 (异常隔离); message 完整写入 AlertLog
        if notifier:
            try:
                threshold = _resolve_rsi_threshold(config)
                message = format_ndx_grid_entry(
                    new_cycle, current_price, indicators=ndx_data, rsi_threshold=threshold
                )
                _notify_with_log(db, notifier, message, "NDX_GRID_ENTRY", "NDX Grid Entry Signal")
            except Exception as e:
                logger.error(f"[NDX Monitor] Failed to send WeChat entry alert for cycle {new_cycle.id}: {e}")

        # Entry 已触发 (RSI <= threshold): 离开 proximity 区域, 允许下个 cycle 重新提醒
        dedup.clear_proximity()

        return {"status": "CREATED_WAITING", "cycle_id": new_cycle.id}

    logger.debug(f"[NDX Monitor] NDX price={current_price:.2f}: No entry signal met.")

    # -------------------------------------------------------------
    # 4. 接近入场阈值提醒 (独立事件, 仅提醒, 不影响状态机)
    #    规则: threshold < RSI <= threshold + PROXIMITY_DISTANCE
    #    去重: 同一 cycle 进入 proximity 区域只提醒一次 (dedup 状态型)
    # -------------------------------------------------------------
    _evaluate_proximity_alert(db, ndx_data, current_price, config, notifier)

    return {"status": "NO_SIGNAL"}


def _evaluate_proximity_alert(
    db: Session,
    ndx_data: Dict[str, Any],
    current_price: float,
    config: Optional[Any],
    notifier: Optional[WeChatNotifier],
) -> None:
    """🟡 接近入场阈值事件: RSI 进入 [threshold+ε, threshold+5] 区域时提醒一次。

    - 只在无 RUNNING / 无 WAITING 时评估 (调用点保证)
    - RSI 离开区域 -> 重置状态, 再次进入可再次提醒
    - Entry 触发 / cycle 结束 -> 状态重置 (由各事件分支调用 clear_proximity)
    """
    rsi = ndx_data.get("rsi")
    if not isinstance(rsi, (int, float)) or isinstance(rsi, bool):
        dedup.mark_proximity_exit()
        return

    threshold = _resolve_rsi_threshold(config)
    in_zone = threshold < rsi <= threshold + PROXIMITY_DISTANCE

    if not in_zone:
        # 离开 proximity 区域 (含 RSI <= threshold: 已触发/已越过)
        dedup.mark_proximity_exit()
        return

    if notifier is None:
        return

    if not dedup.should_alert_proximity():
        # 同一 cycle 已提醒过, 防止 scheduler 每 5 分钟重复推送
        return

    try:
        message = format_ndx_grid_proximity(current_price, rsi, threshold, indicators=ndx_data)
        _notify_with_log(db, notifier, message, "NDX_GRID_PROXIMITY", "NDX Grid Proximity Alert")
    except Exception as e:
        logger.error(f"[NDX Monitor] Failed to send proximity alert: {e}")
