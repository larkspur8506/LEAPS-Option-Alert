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


def send_daily_report_job(data_fetcher: DataFetcher, db, config):
    logger.info("Generating daily report...")
    if not is_trading_time():
        # Optional: could check if market was open today, but this runs at 16:15 so it's fine.
        pass

    qqq_data = data_fetcher.get_qqq_data()
    if not qqq_data.get("last_price"):
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
        "date": datetime.now().strftime("%Y-%m-%d"),
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
    # bypass dedup or use a special dedup key
    if dedup.should_alert("DAILY_REPORT"):
        notifier.send_daily_report(report_data)


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
        send_daily_report_job,
        "cron",
        hour=16,
        minute=30,
        day_of_week='mon-fri',
        timezone="America/New_York",
        args=[data_fetcher, db, config],
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
