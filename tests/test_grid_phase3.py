"""
Unit Tests for Phase 3:
- NDX Periodic Monitoring
- Entry Signal Trigger & WAITING Creation
- RUNNING Boundary Monitoring (Upper >= actual_upper -> CLOSED, Lower < actual_lower -> STOPPED)
- WeChat Notification Dispatch & Idempotency
- WeChat Failure Resilience (no DB rollback)
- Market Data Fetch Failure Resilience
"""
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from pytz import timezone

from app.database.models import Base, GridCycle
from app.alerts.grid_cycle import (
    create_waiting_grid_cycle,
    start_grid_cycle,
    get_running_grid_cycle,
    get_waiting_grid_cycle
)
from app.alerts.grid_monitor import process_ndx_grid_cycle, _is_data_fresh
from app.scheduler.jobs import check_ndx_grid_cycles
from app.scheduler.trading_hours import get_latest_trading_day
from app.notification.wechat import WeChatNotifier

et_tz = timezone("America/New_York")


def make_fresh_timestamp() -> str:
    """生成最近一个 NYSE 交易日的 data_timestamp 字符串（模拟 data_fetcher 输出格式）"""
    return f"{get_latest_trading_day().isoformat()} 16:00:00-04:00"


class TestGridPhase3(unittest.TestCase):
    def setUp(self):
        """每个测试用例使用独立的 SQLite 内存数据库"""
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

        # 最近交易日的 timestamp，保证既有测试数据默认通过 freshness 检查
        self.fresh_ts = datetime.now(et_tz)

        # 构造通用的有效 NDX 开仓数据
        self.valid_entry_ndx_data = {
            "ticker": "^NDX",
            "is_data_valid": True,
            "last_price": 20000.0,
            "rsi": 30.0,  # < 35
            "is_above_sma200_3d": True,
            "price_1y_ago": 18000.0,  # 20000 > 18000
            "price_1y_ago_available": True,
            "ma200": 19000.0,
            "data_timestamp": str(self.fresh_ts)
        }

        # 构造未达开仓标准的 NDX 数据 (RSI=50 >= 35)
        self.no_entry_ndx_data = {
            "ticker": "^NDX",
            "is_data_valid": True,
            "last_price": 20000.0,
            "rsi": 50.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": 18000.0,
            "price_1y_ago_available": True,
            "ma200": 19000.0,
            "data_timestamp": str(self.fresh_ts)
        }

        # Mock DataFetcher
        self.mock_fetcher = MagicMock()
        # Mock Notifier (grid_monitor 统一走 format_*(模块级) + send_message + AlertLog)
        self.mock_notifier = MagicMock(spec=WeChatNotifier)
        self.mock_notifier.send_message.return_value = True
        self.mock_notifier.send_ndx_entry_alert.return_value = True
        self.mock_notifier.send_ndx_upper_alert.return_value = True
        self.mock_notifier.send_ndx_lower_alert.return_value = True

        # Mock Config
        self.mock_config = MagicMock()
        self.mock_config.get_wechat_webhook_url.return_value = "https://mock.webhook.url"
        self.mock_config.get_rsi_threshold.return_value = 35.0
        self.mock_config.get_default_grid_upper_pct.return_value = 0.20
        self.mock_config.get_default_grid_lower_pct.return_value = 0.20
        self.mock_config.get_default_grid_count.return_value = 200
        self.mock_config.get_default_grid_leverage.return_value = 5.0

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    # -------------------------------------------------------------
    # Entry Tests (1-4)
    # -------------------------------------------------------------
    def test_1_entry_signal_creates_waiting(self):
        """1. 无 RUNNING、无 WAITING、signal=True -> 创建 WAITING"""
        self.mock_fetcher.get_ndx_data.return_value = self.valid_entry_ndx_data

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )

        self.assertEqual(res["status"], "CREATED_WAITING")
        waiting = get_waiting_grid_cycle(self.db)
        self.assertIsNotNone(waiting)
        self.assertEqual(waiting.status, "WAITING")
        self.assertEqual(waiting.suggested_base_price, 20000.0)
        self.assertEqual(waiting.suggested_upper_price, 24000.0)
        self.assertEqual(waiting.suggested_lower_price, 16000.0)
        self.assertEqual(waiting.suggested_grid_count, 200)
        self.assertEqual(waiting.suggested_leverage, 5.0)
        self.assertIsNone(waiting.actual_base_price)

    def test_2_entry_no_signal_does_not_create(self):
        """2. 无 RUNNING、无 WAITING、signal=False -> 不创建"""
        self.mock_fetcher.get_ndx_data.return_value = self.no_entry_ndx_data

        res = check_ndx_grid_cycles(
            self.mock_fetcher,
            db=self.db,
            config=self.mock_config,
            check_trading_hours=False
        )

        self.assertEqual(res["status"], "NO_SIGNAL")
        self.assertEqual(self.db.query(GridCycle).count(), 0)

    def test_3_existing_waiting_does_not_create_second(self):
        """3. 已有 WAITING、signal=True -> 不创建第二个"""
        # 先手动创建一个 WAITING
        suggested = {
            "base_price": 19500.0,
            "upper_price": 23400.0,
            "lower_price": 15600.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        first_cycle = create_waiting_grid_cycle(self.db, suggested)

        self.mock_fetcher.get_ndx_data.return_value = self.valid_entry_ndx_data

        res = check_ndx_grid_cycles(
            self.mock_fetcher,
            db=self.db,
            config=self.mock_config,
            check_trading_hours=False
        )

        self.assertEqual(res["status"], "WAITING_EXISTS")
        self.assertEqual(self.db.query(GridCycle).count(), 1)
        self.assertEqual(get_waiting_grid_cycle(self.db).id, first_cycle.id)

    def test_4_existing_running_does_not_create_waiting(self):
        """4. 已有 RUNNING、signal=True -> 不创建 WAITING"""
        # 创建并启动一个 RUNNING 周期
        suggested = {
            "base_price": 20000.0,
            "upper_price": 24000.0,
            "lower_price": 16000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(
            self.db,
            cycle.id,
            actual_base_price=20000.0,
            actual_upper_price=24000.0,
            actual_lower_price=16000.0,
            actual_grid_count=200,
            actual_leverage=5.0
        )

        # 市场价格在正常区间内，但指标满足开仓条件
        ndx_data = dict(self.valid_entry_ndx_data)
        ndx_data["last_price"] = 20500.0
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        res = check_ndx_grid_cycles(
            self.mock_fetcher,
            db=self.db,
            config=self.mock_config,
            check_trading_hours=False
        )

        self.assertEqual(res["status"], "RUNNING_NO_CHANGE")
        self.assertIsNone(get_waiting_grid_cycle(self.db))
        self.assertEqual(self.db.query(GridCycle).count(), 1)

    # -------------------------------------------------------------
    # Upper Tests (5-7)
    # -------------------------------------------------------------
    def test_5_running_current_equals_actual_upper_triggers_closed(self):
        """5. RUNNING + current == actual_upper -> CLOSED (UPPER_REACHED)"""
        suggested = {
            "base_price": 20000.0,
            "upper_price": 24000.0,
            "lower_price": 16000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(
            self.db,
            cycle.id,
            actual_base_price=20000.0,
            actual_upper_price=24000.0,
            actual_lower_price=16000.0,
            actual_grid_count=200,
            actual_leverage=5.0
        )

        ndx_data = {
            "is_data_valid": True,
            "last_price": 24000.0,  # current == actual_upper
            "data_timestamp": str(self.fresh_ts)
        }
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )

        self.assertEqual(res["status"], "CLOSED")
        self.assertEqual(res["reason"], "UPPER_REACHED")

        self.db.refresh(cycle)
        self.assertEqual(cycle.status, "CLOSED")
        self.assertEqual(cycle.close_reason, "UPPER_REACHED")
        self.assertIsNotNone(cycle.closed_at)

    def test_6_running_current_greater_than_actual_upper_triggers_closed(self):
        """6. RUNNING + current > actual_upper -> CLOSED (UPPER_REACHED)"""
        suggested = {
            "base_price": 20000.0,
            "upper_price": 24000.0,
            "lower_price": 16000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(
            self.db,
            cycle.id,
            actual_base_price=20000.0,
            actual_upper_price=24000.0,
            actual_lower_price=16000.0,
            actual_grid_count=200,
            actual_leverage=5.0
        )

        ndx_data = {
            "is_data_valid": True,
            "last_price": 24500.0,  # current > actual_upper
            "data_timestamp": str(self.fresh_ts)
        }
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )

        self.assertEqual(res["status"], "CLOSED")
        self.db.refresh(cycle)
        self.assertEqual(cycle.status, "CLOSED")
        self.assertEqual(cycle.close_reason, "UPPER_REACHED")

    def test_7_closed_cycle_subsequent_runs_no_repeat_notification(self):
        """7. CLOSED 再次运行 Scheduler -> 不重复通知 (幂等保护)"""
        suggested = {
            "base_price": 20000.0,
            "upper_price": 24000.0,
            "lower_price": 16000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(
            self.db,
            cycle.id,
            actual_base_price=20000.0,
            actual_upper_price=24000.0,
            actual_lower_price=16000.0,
            actual_grid_count=200,
            actual_leverage=5.0
        )

        # 第一次运行：触发上限
        ndx_data = {"is_data_valid": True, "last_price": 24200.0, "data_timestamp": str(self.fresh_ts)}
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )

        self.assertEqual(self.mock_notifier.send_message.call_count, 1)

        # 第二次运行 (例如 5 分钟后，价格依然在高位 24300，但无开仓信号)
        ndx_data_run2 = dict(self.no_entry_ndx_data)
        ndx_data_run2["last_price"] = 24300.0
        self.mock_fetcher.get_ndx_data.return_value = ndx_data_run2

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res2 = check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )

        # 不再有 RUNNING 周期，因此不会再次发送 upper alert
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)
        self.assertEqual(res2["status"], "NO_SIGNAL")

    # -------------------------------------------------------------
    # Lower Tests (8-9)
    # -------------------------------------------------------------
    def test_8_running_current_less_than_actual_lower_triggers_stopped(self):
        """8. RUNNING + current < actual_lower -> STOPPED (LOWER_BREACHED)"""
        suggested = {
            "base_price": 20000.0,
            "upper_price": 24000.0,
            "lower_price": 16000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(
            self.db,
            cycle.id,
            actual_base_price=20000.0,
            actual_upper_price=24000.0,
            actual_lower_price=16000.0,
            actual_grid_count=200,
            actual_leverage=5.0
        )

        ndx_data = {
            "is_data_valid": True,
            "last_price": 15999.0,  # current < actual_lower
            "data_timestamp": str(self.fresh_ts)
        }
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )

        self.assertEqual(res["status"], "STOPPED")
        self.assertEqual(res["reason"], "LOWER_BREACHED")

        self.db.refresh(cycle)
        self.assertEqual(cycle.status, "STOPPED")
        self.assertEqual(cycle.close_reason, "LOWER_BREACHED")
        self.assertIsNotNone(cycle.closed_at)

    def test_9_running_current_equals_actual_lower_does_not_trigger_stopped(self):
        """9. RUNNING + current == actual_lower -> 不触发 LOWER_BREACHED (严格要求 < lower)"""
        suggested = {
            "base_price": 20000.0,
            "upper_price": 24000.0,
            "lower_price": 16000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(
            self.db,
            cycle.id,
            actual_base_price=20000.0,
            actual_upper_price=24000.0,
            actual_lower_price=16000.0,
            actual_grid_count=200,
            actual_leverage=5.0
        )

        ndx_data = {
            "is_data_valid": True,
            "last_price": 16000.0,  # current == actual_lower
            "data_timestamp": str(self.fresh_ts)
        }
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        res = check_ndx_grid_cycles(
            self.mock_fetcher,
            db=self.db,
            config=self.mock_config,
            check_trading_hours=False
        )

        self.assertEqual(res["status"], "RUNNING_NO_CHANGE")
        self.db.refresh(cycle)
        self.assertEqual(cycle.status, "RUNNING")
        self.assertIsNone(cycle.closed_at)

    # -------------------------------------------------------------
    # Notification Tests (10-13)
    # -------------------------------------------------------------
    def test_10_entry_signal_sends_wechat_notification(self):
        """10. Entry signal 创建 WAITING 后发送一次开仓通知"""
        self.mock_fetcher.get_ndx_data.return_value = self.valid_entry_ndx_data

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )

        self.mock_notifier.send_message.assert_called_once()
        args, _ = self.mock_notifier.send_message.call_args
        message = args[0]
        self.assertIn("NDX Grid 入场信号", message)
        self.assertIn("20,000.00", message)
        self.assertIn("RSI(14)：30.00", message)

    def test_11_upper_trigger_sends_wechat_notification(self):
        """11. Upper trigger 发送一次通知"""
        suggested = {
            "base_price": 20000.0,
            "upper_price": 24000.0,
            "lower_price": 16000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(
            self.db,
            cycle.id,
            actual_base_price=20000.0,
            actual_upper_price=24000.0,
            actual_lower_price=16000.0,
            actual_grid_count=200,
            actual_leverage=5.0
        )

        self.mock_fetcher.get_ndx_data.return_value = {"is_data_valid": True, "last_price": 24100.0, "data_timestamp": str(self.fresh_ts)}

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )

        self.mock_notifier.send_message.assert_called_once()
        args, _ = self.mock_notifier.send_message.call_args
        message = args[0]
        self.assertIn("已触及上限", message)
        self.assertIn("CLOSED", message)

    def test_12_lower_trigger_sends_wechat_notification(self):
        """12. Lower trigger 发送一次通知"""
        suggested = {
            "base_price": 20000.0,
            "upper_price": 24000.0,
            "lower_price": 16000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(
            self.db,
            cycle.id,
            actual_base_price=20000.0,
            actual_upper_price=24000.0,
            actual_lower_price=16000.0,
            actual_grid_count=200,
            actual_leverage=5.0
        )

        self.mock_fetcher.get_ndx_data.return_value = {"is_data_valid": True, "last_price": 15900.0, "data_timestamp": str(self.fresh_ts)}

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )

        self.mock_notifier.send_message.assert_called_once()
        args, _ = self.mock_notifier.send_message.call_args
        message = args[0]
        self.assertIn("已触及下限", message)
        self.assertIn("STOPPED", message)

    def test_13_wechat_failure_does_not_rollback_state_change(self):
        """13. WeChat 发送失败不能导致 GridCycle 状态回滚"""
        suggested = {
            "base_price": 20000.0,
            "upper_price": 24000.0,
            "lower_price": 16000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(
            self.db,
            cycle.id,
            actual_base_price=20000.0,
            actual_upper_price=24000.0,
            actual_lower_price=16000.0,
            actual_grid_count=200,
            actual_leverage=5.0
        )

        # 模拟微信抛出异常或网络超时
        faulty_notifier = MagicMock()
        faulty_notifier.send_message.side_effect = ConnectionError("WeChat network error")

        self.mock_fetcher.get_ndx_data.return_value = {"is_data_valid": True, "last_price": 24500.0, "data_timestamp": str(self.fresh_ts)}

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=faulty_notifier):
            res = check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )

        # 任务成功返回 CLOSED
        self.assertEqual(res["status"], "CLOSED")
        # 数据库状态确保持久化为 CLOSED，未被 rollback
        self.db.refresh(cycle)
        self.assertEqual(cycle.status, "CLOSED")
        self.assertEqual(cycle.close_reason, "UPPER_REACHED")

    # -------------------------------------------------------------
    # Data Failure Tests (14)
    # -------------------------------------------------------------
    def test_14_data_failure_safe_skip(self):
        """14. NDX 数据获取失败 -> 本次任务安全退出，不创建周期，不修改已有周期"""
        from app.alerts.dedup import clear_dedup

        # Case A: 抛出异常
        self.mock_fetcher.get_ndx_data.side_effect = Exception("yfinance service timeout")

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res_err = check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )
        self.assertEqual(res_err["status"], "SKIPPED")
        self.assertEqual(res_err["reason"], "DATA_FETCH_ERROR")
        self.assertEqual(self.db.query(GridCycle).count(), 0)
        # 数据不可用提醒恰好发送一次 (按日去重)
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)

        # Case B: 返回空数据字典 (同一天已提醒过 -> dedup, 不重复推送)
        clear_dedup()
        self.mock_notifier.send_message.reset_mock()
        self.mock_fetcher.get_ndx_data.side_effect = None
        self.mock_fetcher.get_ndx_data.return_value = {}

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res_empty = check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )
        self.assertEqual(res_empty["status"], "SKIPPED")
        self.assertEqual(res_empty["reason"], "NO_VALID_DATA")
        self.assertEqual(self.db.query(GridCycle).count(), 0)
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)

        # Case C: 存在 RUNNING 周期时数据获取失败，RUNNING 周期不被意外修改
        suggested = {
            "base_price": 20000.0,
            "upper_price": 24000.0,
            "lower_price": 16000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        start_grid_cycle(
            self.db,
            cycle.id,
            actual_base_price=20000.0,
            actual_upper_price=24000.0,
            actual_lower_price=16000.0,
            actual_grid_count=200,
            actual_leverage=5.0
        )

        self.mock_fetcher.get_ndx_data.side_effect = Exception("network unavailable")
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(
                self.mock_fetcher,
                db=self.db,
                config=self.mock_config,
                check_trading_hours=False
            )

        self.db.refresh(cycle)
        self.assertEqual(cycle.status, "RUNNING")
        self.assertIsNone(cycle.closed_at)


class TestDataFreshness(unittest.TestCase):
    """Phase 3 数据新鲜度 (freshness) 专项测试"""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

        self.mock_fetcher = MagicMock()
        self.mock_notifier = MagicMock(spec=WeChatNotifier)
        self.mock_config = MagicMock()
        self.mock_config.get_wechat_webhook_url.return_value = None
        self.mock_config.get_rsi_threshold.return_value = 35.0
        self.mock_config.get_default_grid_upper_pct.return_value = 0.20
        self.mock_config.get_default_grid_lower_pct.return_value = 0.20
        self.mock_config.get_default_grid_count.return_value = 200
        self.mock_config.get_default_grid_leverage.return_value = 5.0

        self.fresh_ts = datetime.now(et_tz)
        # 显式过期的数据时间戳 (10 天前)
        self.stale_ts = datetime.now(et_tz) - timedelta(days=10)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _create_running_cycle(self):
        """创建并启动一个 RUNNING 网格周期 (base=20000, upper=24000, lower=16000)"""
        suggested = {
            "base_price": 20000.0,
            "upper_price": 24000.0,
            "lower_price": 16000.0,
            "grid_count": 200,
            "leverage": 5.0
        }
        cycle = create_waiting_grid_cycle(self.db, suggested)
        return start_grid_cycle(
            self.db,
            cycle.id,
            actual_base_price=20000.0,
            actual_upper_price=24000.0,
            actual_lower_price=16000.0,
            actual_grid_count=200,
            actual_leverage=5.0
        )

    # -------------------------------------------------------------
    # Fresh Data
    # -------------------------------------------------------------
    def test_15_fresh_data_proceeds_normal_flow(self):
        """15. 新鲜数据 (最近交易日) + 开仓信号 -> 正常创建 WAITING"""
        ndx_data = {
            "is_data_valid": True,
            "last_price": 20000.0,
            "rsi": 30.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": 18000.0,
            "price_1y_ago_available": True,
            "ma200": 19000.0,
            "data_timestamp": str(self.fresh_ts)
        }
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        res = check_ndx_grid_cycles(
            self.mock_fetcher,
            db=self.db,
            config=self.mock_config,
            check_trading_hours=False
        )

        self.assertEqual(res["status"], "CREATED_WAITING")
        waiting = get_waiting_grid_cycle(self.db)
        self.assertIsNotNone(waiting)
        self.assertEqual(waiting.suggested_leverage, 5.0)

    # -------------------------------------------------------------
    # Stale Data - No RUNNING / No WAITING
    # -------------------------------------------------------------
    def test_16_stale_data_no_running_no_waiting_created(self):
        """16. 陈旧数据 + 无 RUNNING/无 WAITING -> 不创建 WAITING、不发通知"""
        ndx_data = {
            "is_data_valid": True,
            "last_price": 20000.0,
            "rsi": 30.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": 18000.0,
            "price_1y_ago_available": True,
            "ma200": 19000.0,
            "data_timestamp": str(self.stale_ts)
        }
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        res = check_ndx_grid_cycles(
            self.mock_fetcher,
            db=self.db,
            config=self.mock_config,
            check_trading_hours=False
        )

        self.assertEqual(res["status"], "SKIPPED")
        self.assertEqual(res["reason"], "STALE_DATA")
        self.assertEqual(self.db.query(GridCycle).count(), 0)

    def test_17_stale_data_missing_timestamp_treated_as_stale(self):
        """17. 缺失/非法 data_timestamp -> 视为陈旧，安全跳过"""
        # 缺失
        missing = {"is_data_valid": True, "last_price": 20000.0}
        self.assertFalse(_is_data_fresh(missing))

        # 非法字符串
        invalid = {"is_data_valid": True, "last_price": 20000.0, "data_timestamp": "not-a-date"}
        self.assertFalse(_is_data_fresh(invalid))

        # None
        none_ts = {"is_data_valid": True, "last_price": 20000.0, "data_timestamp": None}
        self.assertFalse(_is_data_fresh(none_ts))

    # -------------------------------------------------------------
    # Stale Data - RUNNING Protected
    # -------------------------------------------------------------
    def test_18_stale_data_with_running_keeps_running_state(self):
        """18. 陈旧数据 + RUNNING (价格在区间内) -> RUNNING 状态不变"""
        cycle = self._create_running_cycle()

        ndx_data = {
            "is_data_valid": True,
            "last_price": 20000.0,
            "data_timestamp": str(self.stale_ts)
        }
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        res = check_ndx_grid_cycles(
            self.mock_fetcher,
            db=self.db,
            config=self.mock_config,
            check_trading_hours=False
        )

        self.assertEqual(res["status"], "SKIPPED")
        self.assertEqual(res["reason"], "STALE_DATA")
        self.db.refresh(cycle)
        self.assertEqual(cycle.status, "RUNNING")
        self.assertIsNone(cycle.closed_at)
        self.assertIsNone(cycle.close_reason)

    def test_19_stale_data_does_not_trigger_upper(self):
        """19. 陈旧数据 + RUNNING 且价格高于 upper -> 不得 CLOSED / UPPER_REACHED"""
        cycle = self._create_running_cycle()

        ndx_data = {
            "is_data_valid": True,
            "last_price": 25000.0,  # > actual_upper 24000，但数据陈旧
            "data_timestamp": str(self.stale_ts)
        }
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        res = check_ndx_grid_cycles(
            self.mock_fetcher,
            db=self.db,
            config=self.mock_config,
            check_trading_hours=False
        )

        self.assertEqual(res["status"], "SKIPPED")
        self.assertEqual(res["reason"], "STALE_DATA")
        self.db.refresh(cycle)
        self.assertEqual(cycle.status, "RUNNING")
        self.assertIsNone(cycle.closed_at)
        self.mock_notifier.send_message.assert_not_called()

    def test_20_stale_data_does_not_trigger_lower(self):
        """20. 陈旧数据 + RUNNING 且价格低于 lower -> 不得 STOPPED / LOWER_BREACHED"""
        cycle = self._create_running_cycle()

        ndx_data = {
            "is_data_valid": True,
            "last_price": 15000.0,  # < actual_lower 16000，但数据陈旧
            "data_timestamp": str(self.stale_ts)
        }
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        res = check_ndx_grid_cycles(
            self.mock_fetcher,
            db=self.db,
            config=self.mock_config,
            check_trading_hours=False
        )

        self.assertEqual(res["status"], "SKIPPED")
        self.assertEqual(res["reason"], "STALE_DATA")
        self.db.refresh(cycle)
        self.assertEqual(cycle.status, "RUNNING")
        self.assertIsNone(cycle.closed_at)
        self.mock_notifier.send_message.assert_not_called()

    # -------------------------------------------------------------
    # Weekend / Holiday Handling
    # -------------------------------------------------------------
    def test_21_weekend_friday_data_is_fresh(self):
        """21. 周末没有'今天'的数据 -> 周五(最近交易日)数据不算陈旧"""
        et = et_tz
        # 2026-09-05 周六 / 2026-09-06 周日 (2026-09-07 为 Labor Day 前的周五 09-04 正常交易)
        saturday = datetime(2026, 9, 5, 12, 0, 0, tzinfo=et)
        sunday = datetime(2026, 9, 6, 12, 0, 0, tzinfo=et)
        friday_data = "2026-09-04 16:00:00-04:00"  # 周五收盘数据

        self.assertTrue(_is_data_fresh({"data_timestamp": friday_data}, now=saturday))
        self.assertTrue(_is_data_fresh({"data_timestamp": friday_data}, now=sunday))

        # 周六时只有周四的数据 -> 落后于最近交易日 (周五)，视为陈旧
        thursday_data = "2026-09-03 16:00:00-04:00"
        self.assertFalse(_is_data_fresh({"data_timestamp": thursday_data}, now=saturday))

    def test_22_holiday_weekend_gap_detects_stale(self):
        """22. 长周末后 (含 Labor Day) 仍持有节前数据 -> 判定陈旧"""
        et = et_tz
        # 2026-09-07 周一为 Labor Day (休市)，2026-09-08 周二恢复交易
        wednesday = datetime(2026, 9, 9, 12, 0, 0, tzinfo=et)
        labor_day_friday_data = "2026-09-04 16:00:00-04:00"  # 节前周五的数据

        self.assertFalse(_is_data_fresh({"data_timestamp": labor_day_friday_data}, now=wednesday))

    def test_23_freshness_gate_runs_before_entry_signal(self):
        """23. freshness 检查先于 ENTRY 判断: 陈旧数据即使满足全部开仓条件也不创建 WAITING"""
        ndx_data = {
            "ticker": "^NDX",
            "is_data_valid": True,
            "last_price": 20000.0,
            "rsi": 25.0,
            "is_above_sma200_3d": True,
            "price_1y_ago": 17000.0,
            "price_1y_ago_available": True,
            "ma200": 19000.0,
            "data_timestamp": str(self.stale_ts)
        }
        self.mock_fetcher.get_ndx_data.return_value = ndx_data

        res = process_ndx_grid_cycle(self.db, ndx_data, notifier=self.mock_notifier, config=self.mock_config)

        self.assertEqual(res["status"], "SKIPPED")
        self.assertEqual(res["reason"], "STALE_DATA")
        self.assertEqual(self.db.query(GridCycle).count(), 0)


if __name__ == "__main__":
    unittest.main()
