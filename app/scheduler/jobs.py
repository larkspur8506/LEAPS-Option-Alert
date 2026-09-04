from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from datetime import datetime
import json
import logging
import time

from .trading_hours import is_trading_time, get_current_time_et
from app.market.polygon_client import CachedPolygonClient
from app.market.data_fetcher import DataFetcher
from app.alerts import qqq_rules, option_rules, dedup
from app.alerts.grid_monitor import process_ndx_grid_cycle
from app.notification.wechat import get_wechat_notifier
from app.config import get_config

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


def check_qqq_and_options(data_fetcher: DataFetcher, db, config):
    if not is_trading_time():
        logger.info("Outside trading hours, skipping checks")
        return

    logger.info("Starting QQQ and options checks...")
    notifier = get_wechat_notifier(config.get_wechat_webhook_url())

    # 1. 获取 QQQ 数据和指标
    qqq_data = data_fetcher.get_qqq_data()

    if qqq_data.get("last_price"):
        # 2. 检查 QQQ 入场信号
        qqq_alerts = qqq_rules.check_all_qqq_rules(qqq_data, config)

        for alert in qqq_alerts:
            # 使用 rule_name 进行每日去重 (每天最多一次买入指令)
            if dedup.should_alert(alert["rule_name"]):
                success = notifier.send_qqq_alert(alert)
                _log_alert(db, alert, success)

    # 3. 检查持仓期权
    from app.database.models import OptionPosition
    positions = db.query(OptionPosition).all()

    for position in positions:
        try:
            position_ticker = option_rules.format_position_ticker(position)
            logger.info(f"Checking position: {position_ticker} (ID: {position.id})")

            # 获取期权当前价格
            current_price = data_fetcher.get_option_current_price(position)

            if current_price is None:
                logger.warning(f"Failed to get price for position {position_ticker}, skipping")
                continue

            # 1. 立即更新并提交当前价格，确保数据一致性
            position.current_price = current_price
            position.last_price_update = get_current_time_et()
            db.commit()
            logger.debug(f"Updated price for {position_ticker} to ${current_price:.2f}")
            
            # 2. 检查出场/风控信号
            result = option_rules.check_position_signals(position, current_price, qqq_data, config)
            
            # 3. 更新 max_profit
            new_max_profit = result.get("new_max_profit", 0.0)
            if new_max_profit > (position.max_profit or 0.0):
                logger.info(f"Updating max_profit for {position_ticker}: {position.max_profit} -> {new_max_profit}")
                position.max_profit = new_max_profit
                db.commit()
            
            # 4. 处理报警
            option_alerts = result.get("alerts", [])
            if option_alerts:
                logger.info(f"Found {len(option_alerts)} alerts for {position_ticker}")
                
            for alert in option_alerts:
                rule_name = alert["rule_name"]

                # 针对每个 position 去重
                if dedup.should_alert(rule_name, position.id):
                    success = notifier.send_option_alert(alert, position_ticker)
                    alert["position_id"] = position.id
                    _log_alert(db, alert, success)
                    logger.info(f"Alert sent for {position_ticker}: {rule_name}")

            # 5. 性能优化：API 频率限制
            time.sleep(1.0)

        except Exception as e:
            logger.error(f"Error processing position {position.id}: {str(e)}", exc_info=True)
            db.rollback()
            continue

    logger.info("Checks completed")


def cleanup_old_data(db, config):
    logger.info("Starting data cleanup...")

    alert_log_retention = config.get_alert_log_retention_days()
    qqq_data_retention = config.get_daily_qqq_data_retention_days()

    from app.database.models import AlertLog, DailyQQQData
    from datetime import timedelta
    from pytz import timezone

    et_tz = timezone("America/New_York")
    cutoff_date = datetime.now(et_tz) - timedelta(days=alert_log_retention)
    qqq_cutoff_date = datetime.now(et_tz) - timedelta(days=qqq_data_retention)

    deleted_alerts = db.query(AlertLog).filter(
        AlertLog.triggered_at < cutoff_date
    ).delete()

    deleted_qqq_data = db.query(DailyQQQData).filter(
        DailyQQQData.fetched_at < qqq_cutoff_date
    ).delete()

    db.commit()

    dedup.reset_daily_dedup()

    logger.info(f"Deleted {deleted_alerts} old alert logs")
    logger.info(f"Deleted {deleted_qqq_data} old QQQ data records")


def _log_alert(db, alert: dict, success: bool):
    from app.database.models import AlertLog

    alert_log = AlertLog(
        alert_type=alert.get("alert_type", "QQQ_DROP"),
        rule_name=alert.get("rule_name", ""),
        message=json.dumps(alert, default=str),
        sent_successfully=success,
        position_id=alert.get("position_id")
    )

    if not success:
        alert_log.error_message = "Failed to send WeChat notification"

    db.add(alert_log)
    db.commit()


def _send_legacy_daily_report(data_fetcher: DataFetcher, db, config, report_date: str):
    """
    传统 QQQ / LEAPS 日报 (Phase 6 前的原逻辑, 原样保留)。
    daily_report_mode = legacy 时调用。
    """
    logger.info("Generating legacy daily report...")
    if not is_trading_time():
        # Optional: could check if market was open today, but this runs at 16:15 so it's fine.
        pass

    qqq_data = data_fetcher.get_qqq_data()
    if not qqq_data.get("last_price"):
        logger.warning(f"DAILY_REPORT mode=legacy date={report_date} skipped: no QQQ data")
        return

    from app.database.models import OptionPosition
    positions_count = db.query(OptionPosition).count()

    # Determine unmet conditions
    unmet = []
    if qqq_data.get("rsi", 100) >= 35:
        unmet.append(f"RSI({qqq_data.get('rsi',0):.1f}) >= 35")
    if not qqq_data.get("is_above_sma200_3d"):
        unmet.append("未连续3天站上SMA200")
    if qqq_data.get("last_price", 0) <= qqq_data.get("price_1y_ago", 0):
        unmet.append("当前价低于1年前")

    entry_met = len(unmet) == 0

    report_data = {
        "date": report_date,
        "qqq_price": qqq_data.get("last_price"),
        "sma200": qqq_data.get("ma200"),
        "consecutive_days": qqq_data.get("consec_above") if qqq_data.get("last_price") > qqq_data.get("ma200") else qqq_data.get("consec_below"),
        "price_1y": qqq_data.get("price_1y_ago"),
        "rsi": qqq_data.get("rsi"),
        "entry_met": entry_met,
        "unmet_conditions": "，".join(unmet),
        "current_positions": positions_count,
        "max_positions": 5, # default
        "stop_warning": qqq_data.get("is_below_sma200_3d", False)
    }

    notifier = get_wechat_notifier(config.get_wechat_webhook_url())
    # 共享 dedup key "DAILY_REPORT": 同一天无论 mode 如何切换, 最多发送一份日报
    if dedup.should_alert("DAILY_REPORT"):
        success = notifier.send_daily_report(report_data)

        # 记录到数据库
        alert_dict = {
            "alert_type": "DAILY_REPORT",
            "rule_name": "盘后交易日报",
            "message": f"QQQ收盘价: ${report_data['qqq_price']:.2f} | RSI: {report_data['rsi']:.1f} | 均线距离连续: {report_data['consecutive_days']}天"
        }
        _log_alert(db, alert_dict, success)
        logger.info(f"DAILY_REPORT mode=legacy date={report_date} sent={success}")
    else:
        logger.info(f"DAILY_REPORT mode=legacy date={report_date} deduplicated")


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

    report_data = {
        "date": report_date,
        "dashboard": dashboard,
        "latest_cycle": latest_cycle,
        "strategy": strategy,
    }

    webhook = ""
    if config and hasattr(config, "get_wechat_webhook_url"):
        webhook = config.get_wechat_webhook_url()
    notifier = get_wechat_notifier(webhook)

    # 共享 dedup key "DAILY_REPORT" (与 legacy 相同): 一天最多一份, 切换模式不重复发送
    if dedup.should_alert("DAILY_REPORT"):
        success = notifier.send_ndx_grid_report(report_data)
        grid_status = (dashboard.get("running_cycle") or {}).get("status") \
            or (dashboard.get("waiting_cycle") or {}).get("status") \
            or (latest_cycle or {}).get("status") or "NO_ACTIVE_GRID"
        _log_alert(db, {
            "alert_type": "NDX_GRID_DAILY_REPORT",
            "rule_name": "NDX Grid Daily Report",
            "message": f"NDX Grid Daily Report {report_date}: grid_status={grid_status}"
        }, success)
        logger.info(f"DAILY_REPORT mode=ndx_grid date={report_date} sent={success}")
    else:
        logger.info(f"DAILY_REPORT mode=ndx_grid date={report_date} deduplicated")


def send_daily_report_job(data_fetcher: DataFetcher, db=None, config=None):
    """
    每日 16:30 日报入口 (Phase 6)。
    读取 daily_report_mode 运行时配置: off / legacy / ndx_grid, 每天只发送一种。
    默认 legacy (向后兼容, 升级后不改变原行为)。
    任何异常只影响本次日报, 不影响其他 scheduler job。
    """
    close_session = False
    session = db
    if session is None:
        from app.database.init_db import SessionLocal
        session = SessionLocal()
        close_session = True

    mode = "legacy"
    try:
        if config and hasattr(config, "get_daily_report_mode"):
            configured = config.get_daily_report_mode()
            if configured in ("off", "legacy", "ndx_grid"):
                mode = configured
    except Exception as e:
        logger.error(f"DAILY_REPORT failed to read mode, fallback to legacy: {e}")

    report_date = get_current_time_et().strftime("%Y-%m-%d")

    try:
        if mode == "off":
            logger.info(f"DAILY_REPORT mode=off date={report_date} skipped")
            return {"status": "SKIPPED", "reason": "REPORT_DISABLED"}

        if mode == "ndx_grid":
            _send_ndx_grid_daily_report(data_fetcher, session, config, report_date)
        else:
            _send_legacy_daily_report(data_fetcher, session, config, report_date)

        return {"status": "OK", "mode": mode, "date": report_date}
    except Exception as e:
        logger.error(f"DAILY_REPORT mode={mode} date={report_date} failed: {e}", exc_info=True)
        return {"status": "ERROR", "mode": mode, "error": str(e)}
    finally:
        if close_session:
            session.close()


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
        try:
            ndx_data = data_fetcher.get_ndx_data()
        except Exception as e:
            logger.error(f"Failed to fetch NDX data: {e}", exc_info=True)
            return {"status": "SKIPPED", "reason": "DATA_FETCH_ERROR"}

        if not ndx_data or not ndx_data.get("last_price") or ndx_data.get("last_price", 0) <= 0:
            logger.warning("NDX market data unavailable or invalid, skipping cycle check.")
            return {"status": "SKIPPED", "reason": "NO_VALID_DATA"}

        # 2. 获取 notifier
        notifier = None
        if config and hasattr(config, "get_wechat_webhook_url"):
            webhook_url = config.get_wechat_webhook_url()
            if webhook_url:
                notifier = get_wechat_notifier(webhook_url)

        # 3. 驱动网格监控状态机
        result = process_ndx_grid_cycle(session, ndx_data, notifier=notifier, config=config)
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
        check_qqq_and_options,
        "interval",
        minutes=5,
        args=[data_fetcher, db, config],
        id="check_qqq_and_options",
        name="Check QQQ and Options",
        replace_existing=True
    )

    scheduler.add_job(
        check_ndx_grid_cycles,
        "interval",
        minutes=5,
        # db=None: NDX job 自建自关独立 session。
        # 两个 5 分钟 job 由 2-worker 线程池并发执行,
        # 不能与 check_qqq_and_options 共享同一个 Session (非线程安全)。
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
