from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from datetime import datetime
import logging

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
            # 使用 rule_name 进行去重
            if dedup.should_alert(alert["rule_name"]):
                success = notifier.send_qqq_alert(alert)
                _log_alert(db, alert, success)

    # 3. 检查持仓期权
    from app.database.models import OptionPosition
    positions = db.query(OptionPosition).all()

    for position in positions:
        try:
            # 获取期权当前价格
            current_price = data_fetcher.get_option_current_price(position)

            if current_price is None:
                logger.warning(f"Failed to get price for position {position.id}, skipping")
                continue

            # 更新当前价格
            position.current_price = current_price
            position.last_price_update = get_current_time_et()
            
            # 4. 检查出场/风控信号 (Task 3 Logic)
            # check_position_signals 返回 {'alerts':List, 'new_max_profit':float}
            result = option_rules.check_position_signals(position, current_price, qqq_data, config)
            
            # 更新 max_profit
            new_max_profit = result.get("new_max_profit", 0.0)
            if new_max_profit > getattr(position, "max_profit", 0.0):
                position.max_profit = new_max_profit
            
            db.commit()

            # 处理报警
            option_alerts = result.get("alerts", [])
            for alert in option_alerts:
                position_ticker = option_rules.format_position_ticker(position)
                rule_name = alert["rule_name"]

                # 针对每个 position 去重
                if dedup.should_alert(rule_name, position.id):
                    success = notifier.send_option_alert(alert, position_ticker)
                    alert["position_id"] = position.id
                    _log_alert(db, alert, success)

        except Exception as e:
            logger.error(f"Error processing position {position.id}: {e}")
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
    from sqlalchemy import or_

    alert_log = AlertLog(
        alert_type=alert.get("alert_type", "QQQ_DROP"),
        rule_name=alert.get("rule_name", ""),
        message=str(alert),
        sent_successfully=success,
        position_id=alert.get("position_id")
    )

    if not success:
        alert_log.error_message = "Failed to send WeChat notification"

    db.add(alert_log)
    db.commit()


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
