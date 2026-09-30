from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from datetime import datetime, timezone
import logging

from .trading_hours import is_trading_time, get_current_time_et, is_trading_day
from app.market.data_fetcher import DataFetcher
from app.alerts import dedup
from app.alerts.alert_log import log_alert, alerted_within, utcnow_naive
from app.alerts.leaps_monitor import process_leaps_position
from app.notification.wechat import (
    get_wechat_notifier,
    format_leaps_data_stale,
    format_leaps_data_unavailable,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


scheduler = BackgroundScheduler(
    executors={"default": ThreadPoolExecutor(max_workers=2)},
    job_defaults={
        "coalesce": False,
        "max_instances": 1,
        "misfire_grace_time": 300
    }
)


def cleanup_old_data(db, config):
    logger.info("Starting data cleanup...")

    alert_log_retention = config.get_alert_log_retention_days()

    from app.database.models import AlertLog
    from datetime import timedelta

    # 时间基准统一为 UTC naive (AlertLog.triggered_at 的存储口径)
    cutoff_date = utcnow_naive() - timedelta(days=alert_log_retention)

    deleted_alerts = db.query(AlertLog).filter(
        AlertLog.triggered_at < cutoff_date
    ).delete()

    db.commit()

    dedup.reset_daily_dedup()

    logger.info(f"Deleted {deleted_alerts} old alert logs")


def _send_leaps_daily_report(data_fetcher: DataFetcher, db, config, report_date: str):
    """QQQ LEAPS 每日简报 (16:30 ET)。复用 leaps dashboard 聚合, 只做展示格式化。"""
    from app.services import leaps_service

    logger.info("Generating LEAPS daily report...")

    # QQQ 数据异常时仍尽可能发送日报 (dashboard 会将缺失字段渲染为 N/A)
    try:
        qqq_data = data_fetcher.get_qqq_data()
    except Exception as e:
        logger.warning(f"DAILY_REPORT qqq data fetch failed: {e}")
        qqq_data = None

    # 日报涨跌幅: 注入 prev_close (辅助展示, 缺失时日报不显示涨跌幅)
    if isinstance(qqq_data, dict):
        qqq_data["prev_close"] = qqq_data.get("prev_close")

    # 辅助市场指标 (S&P 500 / VIX): 缺失/失败不阻塞日报, 日报渲染 N/A
    try:
        breadth = data_fetcher.get_market_breadth()
    except Exception as e:
        logger.warning(f"DAILY_REPORT breadth data fetch failed: {e}")
        breadth = None
    if not isinstance(breadth, dict):
        breadth = None

    dashboard = leaps_service.get_leaps_dashboard(db, qqq_data)

    # 策略参数 (来自运行时配置, 与 rules 页展示一致)
    strategy = {
        "entry_rsi": 35.0, "tp_rsi": 65.0,
        "time_stop_days": 126, "dte_force_days": 180,
        "target_delta": 0.65, "target_tenor_days": 730,
    }
    if config:
        try:
            strategy = {
                "entry_rsi": config.get_entry_rsi_threshold(),
                "tp_rsi": config.get_tp_rsi(),
                "time_stop_days": config.get_time_stop_trading_days(),
                "dte_force_days": config.get_dte_force_days(),
                "target_delta": config.get_target_delta(),
                "target_tenor_days": config.get_target_tenor_days(),
            }
        except Exception as e:
            logger.warning(f"DAILY_REPORT strategy config fallback to defaults: {e}")

    report_data = {
        "date": report_date,
        "dashboard": dashboard,
        "strategy": strategy,
        "breadth": breadth,
    }

    webhook = ""
    if config and hasattr(config, "get_wechat_webhook_url"):
        webhook = config.get_wechat_webhook_url()
    notifier = get_wechat_notifier(webhook)

    # 去重 (两道): 内存态 (同进程) + 落库态 (跨重启, 修复"16:30 后重启当天重发日报")
    # 落库闸门以"美东自然日"为界: 若用滚动 24 小时窗口, 昨天的日报(约 24 小时前)
    # 会被判成"今天已发", 导致隔天漏发日报。
    day_start_et = get_current_time_et().replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_et.astimezone(timezone.utc).replace(tzinfo=None)
    already_sent_today = alerted_within(
        db, "LEAPS_DAILY_REPORT", since_utc=day_start_utc
    )
    if not already_sent_today and dedup.should_alert("DAILY_REPORT"):
        # 先格式化完整日报文本 (与企业微信实际发送内容相同的唯一来源)
        formatted_message = notifier.format_leaps_report(report_data)
        success = notifier.send_leaps_report(report_data)
        # 落库内容 = 实发内容
        log_alert(
            db,
            "LEAPS_DAILY_REPORT",
            "LEAPS Daily Report",
            formatted_message,
            success,
        )
        logger.info(f"DAILY_REPORT mode=leaps date={report_date} sent={success}")
    else:
        logger.info(
            f"DAILY_REPORT mode=leaps date={report_date} deduplicated "
            f"(db_guard={already_sent_today})"
        )


def send_daily_report_job(data_fetcher: DataFetcher, db=None, config=None):
    """
    每日 16:30 日报入口。
    读取 daily_report_mode 运行时配置: off / leaps, 每天只发送一份。
    默认 leaps。任何异常只影响本次日报, 不影响其他 scheduler job。
    美国市场全天休市日 (周末/节假日) 不生成、不发送日报;
    提前收市日仍是有效交易日, 正常发送日报。
    """
    if not is_trading_day():
        logger.info("Daily report skipped: US market is closed today")
        return {"status": "SKIPPED", "reason": "MARKET_CLOSED"}

    close_session = False
    session = db
    if session is None:
        from app.database.init_db import SessionLocal
        session = SessionLocal()
        close_session = True

    mode = "leaps"
    try:
        if config and hasattr(config, "get_daily_report_mode"):
            configured = config.get_daily_report_mode()
            if configured in ("off", "leaps"):
                mode = configured
    except Exception as e:
        logger.error(f"DAILY_REPORT failed to read mode, fallback to leaps: {e}")

    report_date = get_current_time_et().strftime("%Y-%m-%d")

    try:
        if mode == "off":
            logger.info(f"DAILY_REPORT mode=off date={report_date} skipped")
            return {"status": "SKIPPED", "reason": "REPORT_DISABLED"}

        _send_leaps_daily_report(data_fetcher, session, config, report_date)

        return {"status": "OK", "mode": mode, "date": report_date}
    except Exception as e:
        logger.error(f"DAILY_REPORT mode={mode} date={report_date} failed: {e}", exc_info=True)
        return {"status": "ERROR", "mode": mode, "error": str(e)}
    finally:
        if close_session:
            session.close()


def _send_data_unavailable_alert(db, config):
    """⚠️ 数据不可用事件: fail-closed 提醒 (按日去重, 不阻塞监控主流程)"""
    try:
        if not (config and hasattr(config, "get_wechat_webhook_url")):
            return
        webhook_url = config.get_wechat_webhook_url()
        if not webhook_url:
            return

        # 按日去重: 同一天只提醒一次, 防止每 5 分钟重复推送
        if not dedup.should_alert("LEAPS_DATA_UNAVAILABLE"):
            return

        notifier = get_wechat_notifier(webhook_url)
        message = format_leaps_data_unavailable()
        try:
            success = notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send data unavailable alert: {e}")
            success = False
        log_alert(db, "LEAPS_DATA_UNAVAILABLE", "QQQ Data Unavailable", message, success)
    except Exception as e:
        logger.warning(f"data unavailable alert skipped: {e}")


def _send_data_stale_alert(db, notifier, data_timestamp):
    """⚠️ 数据过期事件: fail-closed 提醒 (按日去重, 不修改任何仓位状态)"""
    try:
        if notifier is None:
            return

        # 按日去重: 同一天只提醒一次
        if not dedup.should_alert("LEAPS_DATA_STALE"):
            return

        message = format_leaps_data_stale(data_timestamp)
        try:
            success = notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send data stale alert: {e}")
            success = False
        log_alert(db, "LEAPS_DATA_STALE", "QQQ Data Stale", message, success)
    except Exception as e:
        logger.warning(f"data stale alert skipped: {e}")


def check_leaps_position(data_fetcher: DataFetcher, db=None, config=None, check_trading_hours: bool = True):
    """
    后台定时任务: QQQ LEAPS 仓位监控与状态机驱动。
    每 5 分钟执行一次。
    """
    if check_trading_hours and not is_trading_time():
        logger.info("Outside trading hours, skipping LEAPS checks")
        return {"status": "SKIPPED", "reason": "OUTSIDE_TRADING_HOURS"}

    close_session = False
    session = db
    if session is None:
        from app.database.init_db import SessionLocal
        session = SessionLocal()
        close_session = True

    try:
        # 1. 获取 QQQ 市场数据
        #    数据异常时 fail-closed: 跳过监控, 不触发任何信号, 仅发送数据异常提醒
        try:
            qqq_data = data_fetcher.get_qqq_data()
        except Exception as e:
            logger.error(f"Failed to fetch QQQ data: {e}", exc_info=True)
            _send_data_unavailable_alert(session, config)
            return {"status": "SKIPPED", "reason": "DATA_FETCH_ERROR"}

        if not qqq_data or not qqq_data.get("last_price") or qqq_data.get("last_price", 0) <= 0:
            logger.warning("QQQ market data unavailable or invalid, skipping position check.")
            _send_data_unavailable_alert(session, config)
            return {"status": "SKIPPED", "reason": "NO_VALID_DATA"}

        # 2. 获取 notifier
        notifier = None
        if config and hasattr(config, "get_wechat_webhook_url"):
            webhook_url = config.get_wechat_webhook_url()
            if webhook_url:
                notifier = get_wechat_notifier(webhook_url)

        # 3. 驱动 LEAPS 监控状态机
        result = process_leaps_position(session, qqq_data, notifier=notifier, config=config)

        # 4. 数据异常事件提醒 (fail-closed: 状态机已跳过监控, 此处仅发送中文提醒)
        if isinstance(result, dict) and result.get("status") == "SKIPPED":
            reason = result.get("reason")
            if reason == "STALE_DATA":
                _send_data_stale_alert(session, notifier, qqq_data.get("data_timestamp"))
            elif reason in ("NO_VALID_DATA", "INVALID_PRICE", "NON_POSITIVE_PRICE"):
                _send_data_unavailable_alert(session, config)

        return result

    except Exception as e:
        # 注意: process_leaps_position 内部的状态变更已各自 commit,
        # 此处禁止 rollback (避免回滚无关的未提交事务); 仅记录并继续下一轮。
        logger.error(f"Unexpected error in check_leaps_position: {e}", exc_info=True)
        return {"status": "ERROR", "error": str(e)}

    finally:
        if close_session:
            session.close()


def start_scheduler(data_fetcher: DataFetcher, db, config):
    scheduler.add_job(
        check_leaps_position,
        "interval",
        minutes=5,
        # db=None: LEAPS job 自建自关独立 session (与 Phase 5 并发修复同款),
        # 避免与其他 job 共享非线程安全 Session。
        args=[data_fetcher, None, config],
        id="check_leaps_position",
        name="Check QQQ LEAPS Position",
        replace_existing=True
    )

    scheduler.add_job(
        send_daily_report_job,
        "cron",
        hour=16,
        minute=30,
        day_of_week='mon-fri',
        timezone="America/New_York",
        args=[data_fetcher, None, config],
        id="send_daily_report",
        name="Send Daily Report",
        replace_existing=True
    )

    scheduler.add_job(
        cleanup_old_data,
        "cron",
        hour=2,
        minute=0,
        args=[db, config],
        id="cleanup_old_data",
        name="Cleanup Old Data",
        replace_existing=True
    )

    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")
