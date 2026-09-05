from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from datetime import datetime
import logging

from .trading_hours import is_trading_time, get_current_time_et
from app.market.data_fetcher import DataFetcher
from app.alerts import dedup
from app.alerts.alert_log import log_alert
from app.alerts.grid_monitor import process_ndx_grid_cycle
from app.notification.wechat import (
    get_wechat_notifier,
    format_ndx_grid_data_stale,
    format_ndx_grid_data_unavailable,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    from pytz import timezone

    et_tz = timezone("America/New_York")
    cutoff_date = datetime.now(et_tz) - timedelta(days=alert_log_retention)

    deleted_alerts = db.query(AlertLog).filter(
        AlertLog.triggered_at < cutoff_date
    ).delete()

    db.commit()

    dedup.reset_daily_dedup()

    logger.info(f"Deleted {deleted_alerts} old alert logs")


def _log_alert(db, alert: dict, success: bool):
    """兼容保留: 旧 dict 接口 -> 统一 AlertLog 写入层 (message=完整文本时走 log_alert)"""
    from app.alerts.alert_log import log_alert_legacy_json

    log_alert_legacy_json(db, alert, success)


def _send_ndx_grid_daily_report(data_fetcher: DataFetcher, db, config, report_date: str):
    """
    NDX Grid 日报 (Phase 6 新增)。
    复用 grid_service.get_grid_dashboard 聚合 (Phase 1-3 已有计算), 只做展示格式化。
    """
    from app.services import grid_service

    logger.info("Generating NDX grid daily report...")

    # NDX 数据异常时仍尽可能发送日报 (dashboard 会将缺失字段渲染为 N/A)
    try:
        ndx_data = data_fetcher.get_ndx_data()
    except Exception as e:
        logger.warning(f"DAILY_REPORT ndx data fetch failed: {e}")
        ndx_data = None

    dashboard = grid_service.get_grid_dashboard(db, ndx_data)

    # 日报涨跌幅: 注入 prev_close (辅助展示, 缺失时日报不显示涨跌幅)
    if isinstance(ndx_data, dict) and isinstance(dashboard.get("ndx"), dict):
        dashboard["ndx"]["prev_close"] = ndx_data.get("prev_close")

    # 辅助市场指标 (S&P 500 / VIX): 缺失/失败不阻塞日报, 日报渲染 N/A
    try:
        breadth = data_fetcher.get_market_breadth()
    except Exception as e:
        logger.warning(f"DAILY_REPORT breadth data fetch failed: {e}")
        breadth = None
    if not isinstance(breadth, dict):
        breadth = None

    latest_cycles = grid_service.get_cycle_history(db, 1)["cycles"]
    latest_cycle = latest_cycles[0] if latest_cycles else None

    # Grid 策略默认参数 (来自运行时配置, 与 rules 页展示一致)
    strategy = {"upper_pct": 0.20, "lower_pct": 0.20, "grid_count": 200, "leverage": 5.0}
    if config:
        try:
            strategy = {
                "upper_pct": config.get_default_grid_upper_pct(),
                "lower_pct": config.get_default_grid_lower_pct(),
                "grid_count": config.get_default_grid_count(),
                "leverage": config.get_default_grid_leverage(),
            }
        except Exception as e:
            logger.warning(f"DAILY_REPORT strategy config fallback to defaults: {e}")
    try:
        strategy["rsi_threshold"] = (
            config.get_rsi_threshold() if config and hasattr(config, "get_rsi_threshold") else 35.0
        )
    except Exception:
        strategy["rsi_threshold"] = 35.0

    report_data = {
        "date": report_date,
        "dashboard": dashboard,
        "latest_cycle": latest_cycle,
        "strategy": strategy,
        "breadth": breadth,
    }

    webhook = ""
    if config and hasattr(config, "get_wechat_webhook_url"):
        webhook = config.get_wechat_webhook_url()
    notifier = get_wechat_notifier(webhook)

    # dedup key "DAILY_REPORT": 一天最多一份日报
    if dedup.should_alert("DAILY_REPORT"):
        # 先格式化完整日报文本 (与企业微信实际发送内容相同的唯一来源)
        formatted_message = notifier.format_ndx_grid_report(report_data)
        success = notifier.send_ndx_grid_report(report_data)
        _log_alert(db, {
            "alert_type": "NDX_GRID_DAILY_REPORT",
            "rule_name": "NDX Grid Daily Report",
            "message": formatted_message  # 与企业微信发送内容完全一致
        }, success)
        logger.info(f"DAILY_REPORT mode=ndx_grid date={report_date} sent={success}")
    else:
        logger.info(f"DAILY_REPORT mode=ndx_grid date={report_date} deduplicated")


def send_daily_report_job(data_fetcher: DataFetcher, db=None, config=None):
    """
    每日 16:30 日报入口。
    读取 daily_report_mode 运行时配置: off / ndx_grid, 每天只发送一份。
    默认 ndx_grid (历史 'legacy' 配置由 Config 读取层归一化为 ndx_grid)。
    任何异常只影响本次日报, 不影响其他 scheduler job。
    """
    close_session = False
    session = db
    if session is None:
        from app.database.init_db import SessionLocal
        session = SessionLocal()
        close_session = True

    mode = "ndx_grid"
    try:
        if config and hasattr(config, "get_daily_report_mode"):
            configured = config.get_daily_report_mode()
            if configured in ("off", "ndx_grid"):
                mode = configured
    except Exception as e:
        logger.error(f"DAILY_REPORT failed to read mode, fallback to ndx_grid: {e}")

    report_date = get_current_time_et().strftime("%Y-%m-%d")

    try:
        if mode == "off":
            logger.info(f"DAILY_REPORT mode=off date={report_date} skipped")
            return {"status": "SKIPPED", "reason": "REPORT_DISABLED"}

        _send_ndx_grid_daily_report(data_fetcher, session, config, report_date)

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
        if not dedup.should_alert("NDX_GRID_DATA_UNAVAILABLE"):
            return

        notifier = get_wechat_notifier(webhook_url)
        message = format_ndx_grid_data_unavailable()
        try:
            success = notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send data unavailable alert: {e}")
            success = False
        log_alert(db, "NDX_GRID_DATA_UNAVAILABLE", "NDX Data Unavailable", message, success)
    except Exception as e:
        logger.warning(f"data unavailable alert skipped: {e}")


def _send_data_stale_alert(db, notifier, data_timestamp):
    """⚠️ 数据过期事件: fail-closed 提醒 (按日去重, 不修改任何 Grid 状态)"""
    try:
        if notifier is None:
            return

        # 按日去重: 同一天只提醒一次
        if not dedup.should_alert("NDX_GRID_DATA_STALE"):
            return

        message = format_ndx_grid_data_stale(data_timestamp)
        try:
            success = notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send data stale alert: {e}")
            success = False
        log_alert(db, "NDX_GRID_DATA_STALE", "NDX Data Stale", message, success)
    except Exception as e:
        logger.warning(f"data stale alert skipped: {e}")


def check_ndx_grid_cycles(data_fetcher: DataFetcher, db=None, config=None, check_trading_hours: bool = True):
    """
    后台定时任务: NDX 合约网格监控与状态机驱动。
    每 5 分钟执行一次。
    """
    if check_trading_hours and not is_trading_time():
        logger.info("Outside trading hours, skipping NDX grid checks")
        return {"status": "SKIPPED", "reason": "OUTSIDE_TRADING_HOURS"}

    close_session = False
    session = db
    if session is None:
        from app.database.init_db import SessionLocal
        session = SessionLocal()
        close_session = True

    try:
        # 1. 获取 NDX 市场数据
        #    数据异常时 fail-closed: 跳过监控, 不触发任何信号, 仅发送数据异常提醒
        try:
            ndx_data = data_fetcher.get_ndx_data()
        except Exception as e:
            logger.error(f"Failed to fetch NDX data: {e}", exc_info=True)
            _send_data_unavailable_alert(session, config)
            return {"status": "SKIPPED", "reason": "DATA_FETCH_ERROR"}

        if not ndx_data or not ndx_data.get("last_price") or ndx_data.get("last_price", 0) <= 0:
            logger.warning("NDX market data unavailable or invalid, skipping cycle check.")
            _send_data_unavailable_alert(session, config)
            return {"status": "SKIPPED", "reason": "NO_VALID_DATA"}

        # 2. 获取 notifier
        notifier = None
        if config and hasattr(config, "get_wechat_webhook_url"):
            webhook_url = config.get_wechat_webhook_url()
            if webhook_url:
                notifier = get_wechat_notifier(webhook_url)

        # 3. 驱动网格监控状态机
        result = process_ndx_grid_cycle(session, ndx_data, notifier=notifier, config=config)

        # 4. 数据异常事件提醒 (fail-closed: 状态机已跳过监控, 此处仅发送中文提醒)
        if isinstance(result, dict) and result.get("status") == "SKIPPED":
            reason = result.get("reason")
            if reason == "STALE_DATA":
                _send_data_stale_alert(session, notifier, ndx_data.get("data_timestamp"))
            elif reason in ("NO_VALID_DATA", "INVALID_PRICE", "NON_POSITIVE_PRICE"):
                _send_data_unavailable_alert(session, config)

        return result

    except Exception as e:
        # 注意: process_ndx_grid_cycle 内部的状态变更已各自 commit,
        # 此处禁止 rollback (避免回滚无关的未提交事务); 仅记录并继续下一轮。
        logger.error(f"Unexpected error in check_ndx_grid_cycles: {e}", exc_info=True)
        return {"status": "ERROR", "error": str(e)}

    finally:
        if close_session:
            session.close()


def start_scheduler(data_fetcher: DataFetcher, db, config):
    scheduler.add_job(
        check_ndx_grid_cycles,
        "interval",
        minutes=5,
        # db=None: NDX job 自建自关独立 session (Phase 5 并发修复), 避免与其他 job 共享非线程安全 Session。
        args=[data_fetcher, None, config],
        id="check_ndx_grid_cycles",
        name="Check NDX Grid Cycles",
        replace_existing=True
    )

    scheduler.add_job(
        send_daily_report_job,
        "cron",
        hour=16,
        minute=30,
        day_of_week='mon-fri',
        timezone="America/New_York",
        # db=None: 与 NDX job 一致, 自建独立 session (Phase 5 并发修复同款)
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
