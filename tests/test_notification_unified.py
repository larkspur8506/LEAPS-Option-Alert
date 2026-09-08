"""
Unified NDX Grid Notification Tests.

Covers (任务 17):
A. Entry notification: 中文/emoji/价格/RSI/阈值/距离/Base/Upper/Lower/网格数/每格/杠杆
B. Proximity notification: 模板 + 进入发送/同 cycle 不重复/离开再进入可再发/Entry 后不再发送
C. STOPPED: 跌破下轨进入风险观察 (≠已止损)/止损提醒线/参数/操作提示
D. CLOSED: Upper/CLOSED/Grid 参数/新 cycle 等待 Entry/不声称自动交易
E. Data stale: STALE/监控跳过/不触发 Grid signal
F. Data unavailable: UNAVAILABLE/监控跳过/不触发 Grid signal
G. AlertLog: message == 实际微信消息; 失败时 success=False + 完整 message
H. Secret redaction: webhook secret 不出现在任何消息中
I. 手动关闭通知 (MANUAL_CLOSE 状态变化)
"""
import unittest
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from pytz import timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, GridCycle, AlertLog
from app.alerts.grid_cycle import create_waiting_grid_cycle, start_grid_cycle
from app.alerts.grid_monitor import process_ndx_grid_cycle
from app.alerts.dedup import clear_dedup, should_alert_proximity, mark_proximity_exit, is_proximity_active
from app.notification.wechat import (
    WeChatNotifier,
    format_ndx_grid_entry,
    format_ndx_grid_proximity,
    format_ndx_grid_stopped,
    format_ndx_grid_stop_loss,
    format_ndx_grid_closed,
    format_ndx_grid_manual_close,
    format_ndx_grid_data_stale,
    format_ndx_grid_data_unavailable,
    format_ndx_grid_daily_report,
    _redact_secrets,
)
from app.scheduler.jobs import check_ndx_grid_cycles
from app.services import grid_service

et_tz = timezone("America/New_York")

SUGGESTED = {
    "base_price": 28920.50,
    "upper_price": 34704.60,
    "lower_price": 23136.40,
    "grid_count": 200,
    "leverage": 5.0,
}

VALID_ACTUAL = {
    "actual_base_price": 28920.50,
    "actual_upper_price": 34704.60,
    "actual_lower_price": 23136.40,
    "actual_grid_count": 200,
    "actual_leverage": 5.0,
    "actual_margin": 10000.0,
}

ENTRY_INDICATORS = {
    "rsi": 34.20,
    "ma200": 27050.30,
    "prev_close": 29463.98,
}


def make_ndx_data(price=28920.50, rsi=50.0, fresh=True, ts=None):
    return {
        "ticker": "^NDX",
        "is_data_valid": True,
        "last_price": price,
        "rsi": rsi,
        "ma200": 27050.30,
        "is_above_sma200_3d": True,
        "price_1y_ago": 28000.0,
        "price_1y_ago_available": True,
        "prev_close": 29100.0,
        "data_timestamp": ts if ts is not None else str(datetime.now(et_tz)),
    }


def make_entry_ndx_data(price=28920.50, rsi=30.0):
    data = make_ndx_data(price=price, rsi=rsi)
    data["is_above_sma200_3d"] = True
    data["price_1y_ago"] = 27000.0
    return data


class NotificationTestBase(unittest.TestCase):
    def setUp(self):
        clear_dedup()
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
        self.mock_notifier.send_message.return_value = True

        self.mock_config = MagicMock()
        self.mock_config.get_wechat_webhook_url.return_value = "https://mock.webhook/key=SECRET"
        self.mock_config.get_rsi_threshold.return_value = 35.0
        self.mock_config.get_default_grid_upper_pct.return_value = 0.20
        self.mock_config.get_default_grid_lower_pct.return_value = 0.20
        self.mock_config.get_default_grid_count.return_value = 200
        self.mock_config.get_default_grid_leverage.return_value = 5.0

    def tearDown(self):
        clear_dedup()
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _alert_logs(self, alert_type):
        return self.db.query(AlertLog).filter(AlertLog.alert_type == alert_type).all()


# ---------------------------------------------------------------------------
# A. Entry notification
# ---------------------------------------------------------------------------
class TestEntryFormatter(NotificationTestBase):
    def test_A_entry_message_complete(self):
        """A. Entry 通知: 中文/emoji/价格/RSI/阈值/Base/Upper/Lower/网格/每格/杠杆"""
        msg = format_ndx_grid_entry(SUGGESTED, 28920.50, indicators=ENTRY_INDICATORS, rsi_threshold=35.0)

        self.assertIn("🚨 NDX Grid 入场信号", msg)
        self.assertIn("价格：28,920.50", msg)
        self.assertIn("-1.84%", msg)             # 涨跌幅 (28920.50 vs prev 29463.98)
        self.assertIn("RSI(14)：34.20", msg)
        self.assertIn("SMA200：27,050.30", msg)
        self.assertIn("距SMA200：+6.91%", msg)
        self.assertIn("入场阈值：35.00", msg)
        self.assertIn("状态：🟢 已触发", msg)
        self.assertIn("Base：28,920.50", msg)
        self.assertIn("Upper：34,704.60", msg)
        self.assertIn("Lower：23,136.40", msg)
        self.assertIn("网格：200格", msg)
        self.assertIn("每格：57.84", msg)         # (34704.60-23136.40)/200
        self.assertIn("杠杆：5.0x", msg)
        self.assertIn("请在交易所手动建立对应 Grid", msg)
        self.assertIn("不连接交易所", msg)

    def test_A2_entry_with_orm_cycle(self):
        """A2. Entry 通知接受 GridCycle ORM 对象 (使用 suggested_*)"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        msg = format_ndx_grid_entry(cycle, 28920.50, indicators=ENTRY_INDICATORS, rsi_threshold=35.0)
        self.assertIn("Base：28,920.50", msg)
        self.assertIn("每格：57.84", msg)

    def test_A3_entry_monitor_integration_alertlog(self):
        """A3. monitor 集成: Entry 触发 -> 发送一次 + AlertLog 与发送内容一致"""
        self.mock_fetcher.get_ndx_data.return_value = make_entry_ndx_data()
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                        check_trading_hours=False)
        self.assertEqual(res["status"], "CREATED_WAITING")
        self.mock_notifier.send_message.assert_called_once()
        sent_message = self.mock_notifier.send_message.call_args[0][0]
        self.assertIn("NDX Grid 入场信号", sent_message)

        logs = self._alert_logs("NDX_GRID_ENTRY")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].message, sent_message)
        self.assertTrue(logs[0].sent_successfully)

    def test_A4_entry_not_repeated_while_running(self):
        """A4. Grid RUNNING 期间不得重复生成建议建立 Grid 的 Entry 通知"""
        self.mock_fetcher.get_ndx_data.return_value = make_entry_ndx_data()
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)

        # 用户启动 Grid (RUNNING), 下一轮监控不再发送 Entry 通知
        waiting = self.db.query(GridCycle).filter(GridCycle.status == "WAITING").first()
        start_grid_cycle(self.db, waiting.id, **dict(
            actual_base_price=VALID_ACTUAL["actual_base_price"],
            actual_upper_price=VALID_ACTUAL["actual_upper_price"],
            actual_lower_price=VALID_ACTUAL["actual_lower_price"],
            actual_grid_count=VALID_ACTUAL["actual_grid_count"],
            actual_leverage=VALID_ACTUAL["actual_leverage"],
        ))
        self.mock_notifier.send_message.reset_mock()

        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(price=29500.0, rsi=33.0)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                        check_trading_hours=False)
        self.assertEqual(res["status"], "RUNNING_NO_CHANGE")
        self.mock_notifier.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# B. Proximity notification
# ---------------------------------------------------------------------------
class TestProximityFormatter(NotificationTestBase):
    def test_B_proximity_message_template(self):
        """B. RSI=37.20, threshold=35.00 -> 🟡 接近触发 / 距离阈值：2.20"""
        msg = format_ndx_grid_proximity(29180.20, 37.20, 35.0, indicators=make_ndx_data(rsi=37.20))
        self.assertIn("🟡 NDX Grid 接近入场", msg)
        self.assertIn("状态：🟡 接近触发", msg)
        self.assertIn("入场阈值：35.00", msg)
        self.assertIn("距离阈值：2.20", msg)
        self.assertIn("RSI(14)：37.20", msg)
        self.assertIn("价格：29,180.20", msg)

    def test_B2_proximity_sent_once_per_zone_entry(self):
        """B2. 进入 proximity 区域发送; 同一 cycle 内 37.5/37.1 不重复发送"""
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(rsi=37.20)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res1 = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                         check_trading_hours=False)
        self.assertEqual(res1["status"], "NO_SIGNAL")
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)
        msg = self.mock_notifier.send_message.call_args[0][0]
        self.assertIn("🟡 接近触发", msg)
        self.assertIn("距离阈值：2.20", msg)

        # 5 分钟后: 37.5 / 37.1 -> 不再发送
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(rsi=37.50)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(rsi=37.10)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)

        logs = self._alert_logs("NDX_GRID_PROXIMITY")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].message, msg)

    def test_B3_proximity_reentry_after_exit(self):
        """B3. RSI 离开区域后再进入 -> 可以再次发送"""
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(rsi=37.20)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)

        # 离开区域 (RSI 45)
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(rsi=45.0)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)

        # 再次进入 (RSI 38.4 -> 距离 3.40)
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(rsi=38.40)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        self.assertEqual(self.mock_notifier.send_message.call_count, 2)
        msg2 = self.mock_notifier.send_message.call_args[0][0]
        self.assertIn("距离阈值：3.40", msg2)

    def test_B4_no_proximity_after_entry_triggered(self):
        """B4. Entry 触发 (创建 WAITING) 后不再发送 proximity"""
        self.mock_fetcher.get_ndx_data.return_value = make_entry_ndx_data(rsi=30.0)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                        check_trading_hours=False)
        self.assertEqual(res["status"], "CREATED_WAITING")

        # WAITING 存在期间 RSI 回到 proximity 区域 -> 不评估 proximity
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(rsi=37.20)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res2 = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                         check_trading_hours=False)
        self.assertEqual(res2["status"], "WAITING_EXISTS")
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)  # 仅 Entry 通知
        self.assertEqual(len(self._alert_logs("NDX_GRID_PROXIMITY")), 0)

    def test_B5_proximity_zone_boundary_rules(self):
        """B5. 边界: RSI=40.00 (threshold+5) 属于区域; RSI=40.01 不属于; RSI=35.00 触发 Entry 不提醒"""
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(rsi=40.0)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)

        mark_proximity_exit()
        self.mock_notifier.send_message.reset_mock()
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(rsi=40.01)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        self.mock_notifier.send_message.assert_not_called()

        # RSI = threshold: Entry 判定边界 (rsi >= threshold 不触发 Entry), proximity 也不含 35.00
        mark_proximity_exit()
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(rsi=35.0)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        # 35.00 不属于 proximity 区域 (threshold < rsi), 无新增 proximity 提醒
        self.assertEqual(len(self._alert_logs("NDX_GRID_PROXIMITY")), 1)  # 仅 rsi=40 那次

    def test_B6_proximity_dedup_state_semantics(self):
        """B6. dedup 状态语义: 首次 True / 再次 False / exit 后 True"""
        self.assertTrue(should_alert_proximity())
        self.assertFalse(should_alert_proximity())
        mark_proximity_exit()
        self.assertFalse(is_proximity_active())
        self.assertTrue(should_alert_proximity())


# ---------------------------------------------------------------------------
# C. STOPPED
# ---------------------------------------------------------------------------
class TestStoppedNotification(NotificationTestBase):
    def _make_running(self):
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        start_grid_cycle(
            self.db, cycle.id,
            actual_base_price=VALID_ACTUAL["actual_base_price"],
            actual_upper_price=VALID_ACTUAL["actual_upper_price"],
            actual_lower_price=VALID_ACTUAL["actual_lower_price"],
            actual_grid_count=VALID_ACTUAL["actual_grid_count"],
            actual_leverage=VALID_ACTUAL["actual_leverage"],
        )
        return cycle

    def test_C_stopped_message_template(self):
        """C. STOPPED 模板: 跌破下轨进入风险观察 (≠已止损)/Lower/止损提醒线/参数/操作提示"""
        cycle = dict(VALID_ACTUAL)
        cycle["close_reason"] = "LOWER_BREACHED"
        indicators = {"rsi": 28.60, "ma200": 26980.20, "prev_close": 23858.66}
        msg = format_ndx_grid_stopped(cycle, 23080.40, indicators=indicators)

        self.assertIn("🛑 NDX Grid 已跌破下轨", msg)
        self.assertIn("进入风险观察区", msg)
        self.assertIn("当前价格：23,080.40", msg)
        self.assertIn("-3.26%", msg)
        self.assertIn("RSI(14)：28.60", msg)
        self.assertIn("Base：28,920.50", msg)
        self.assertIn("Upper：34,704.60", msg)
        self.assertIn("Lower：23,136.40", msg)
        # 止损提醒线 = 23,136.40 × (1 - 0.10) = 20,822.76
        self.assertIn("止损提醒线：20,822.76", msg)
        self.assertIn("网格：200格", msg)
        self.assertIn("杠杆：5.0x", msg)
        self.assertIn("状态：STOPPED", msg)
        self.assertIn("原因：价格跌破 Lower", msg)
        self.assertIn("如果 NDX 从 Lower 继续下跌 10%", msg)
        self.assertIn("系统将发送止损提醒", msg)
        self.assertIn("不会自动平仓", msg)
        self.assertIn("不会自动重新建立 Grid", msg)
        # STOPPED ≠ 已止损: 不声称已触发止损提醒 / 已自动平仓 / 已取消交易所网格
        self.assertNotIn("已触发止损提醒", msg)
        self.assertNotIn("已自动平仓", msg)
        self.assertNotIn("已取消", msg)
        self.assertNotIn("已平仓", msg)

    def test_C2_stopped_monitor_once_with_alertlog(self):
        """C2. RUNNING -> STOPPED 只推送一次; AlertLog 与实际消息一致"""
        self._make_running()
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(price=23000.0)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                        check_trading_hours=False)
        self.assertEqual(res["status"], "STOPPED")
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)
        sent = self.mock_notifier.send_message.call_args[0][0]
        self.assertIn("STOPPED", sent)
        self.assertIn("价格跌破 Lower", sent)

        # 下一轮 (无 RUNNING) 不再发送 STOPPED
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(price=23000.0, rsi=50.0)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)

        logs = self._alert_logs("NDX_GRID_STOPPED")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].message, sent)


# ---------------------------------------------------------------------------
# D. CLOSED
# ---------------------------------------------------------------------------
class TestClosedNotification(NotificationTestBase):
    def _make_running(self):
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        start_grid_cycle(
            self.db, cycle.id,
            actual_base_price=VALID_ACTUAL["actual_base_price"],
            actual_upper_price=VALID_ACTUAL["actual_upper_price"],
            actual_lower_price=VALID_ACTUAL["actual_lower_price"],
            actual_grid_count=VALID_ACTUAL["actual_grid_count"],
            actual_leverage=VALID_ACTUAL["actual_leverage"],
        )
        return cycle

    def test_D_closed_message_template(self):
        """D. CLOSED 模板: Upper/CLOSED/参数/新 cycle 等待 Entry/不声称自动交易"""
        cycle = dict(VALID_ACTUAL)
        cycle["close_reason"] = "UPPER_REACHED"
        indicators = {"rsi": 71.40, "ma200": 28120.60, "prev_close": 34016.13}
        msg = format_ndx_grid_closed(cycle, 34760.30, indicators=indicators)

        self.assertIn("🎉 NDX Grid 已触及上限", msg)
        self.assertIn("价格：34,760.30", msg)
        self.assertIn("RSI(14)：71.40", msg)
        self.assertIn("Base：28,920.50", msg)
        self.assertIn("Upper：34,704.60", msg)
        self.assertIn("Lower：23,136.40", msg)
        self.assertIn("网格：200格", msg)
        self.assertIn("杠杆：5.0x", msg)
        self.assertIn("状态：CLOSED", msg)
        self.assertIn("触发原因：UPPER_REACHED", msg)
        self.assertIn("新的 Grid 不会自动建立", msg)
        self.assertIn("需等待下一次入场信号", msg)
        self.assertNotIn("已自动", msg)
        self.assertNotIn("已平仓", msg)

    def test_D2_closed_monitor_once_with_alertlog(self):
        """D2. RUNNING -> CLOSED 只推送一次; AlertLog 与实际消息一致"""
        self._make_running()
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(price=34710.0)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                        check_trading_hours=False)
        self.assertEqual(res["status"], "CLOSED")
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)
        sent = self.mock_notifier.send_message.call_args[0][0]
        self.assertIn("CLOSED", sent)
        self.assertIn("UPPER_REACHED", sent)

        logs = self._alert_logs("NDX_GRID_CLOSED")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].message, sent)


# ---------------------------------------------------------------------------
# E/F. Data stale / unavailable (fail-closed)
# ---------------------------------------------------------------------------
class TestDataIssueNotifications(NotificationTestBase):
    def test_E_stale_data_alert_and_fail_closed(self):
        """E. 数据过期 -> STALE 提醒 + 监控跳过 + 满足 Entry 条件也不触发信号"""
        stale_ts = str(datetime.now(et_tz) - timedelta(days=10))
        self.mock_fetcher.get_ndx_data.return_value = make_entry_ndx_data(rsi=25.0)
        self.mock_fetcher.get_ndx_data.return_value["data_timestamp"] = stale_ts

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                        check_trading_hours=False)
        self.assertEqual(res["status"], "SKIPPED")
        self.assertEqual(res["reason"], "STALE_DATA")
        self.assertEqual(self.db.query(GridCycle).count(), 0)

        self.mock_notifier.send_message.assert_called_once()
        msg = self.mock_notifier.send_message.call_args[0][0]
        self.assertIn("NDX 行情数据过期", msg)
        self.assertIn("STALE", msg)
        self.assertIn("本次 Grid 监控已跳过", msg)

        logs = self._alert_logs("NDX_GRID_DATA_STALE")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].message, msg)

    def test_E2_stale_alert_deduped_same_day(self):
        """E2. 同一天第二次 stale -> 不重复推送"""
        stale_ts = str(datetime.now(et_tz) - timedelta(days=10))
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(rsi=50.0)
        self.mock_fetcher.get_ndx_data.return_value["data_timestamp"] = stale_ts

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        self.assertEqual(self.mock_notifier.send_message.call_count, 1)

    def test_F_unavailable_data_alert_and_fail_closed(self):
        """F. 数据不可用 -> UNAVAILABLE 提醒 + 监控跳过 + 不触发信号"""
        self.mock_fetcher.get_ndx_data.side_effect = Exception("yfinance down")

        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                        check_trading_hours=False)
        self.assertEqual(res["status"], "SKIPPED")
        self.assertEqual(res["reason"], "DATA_FETCH_ERROR")
        self.assertEqual(self.db.query(GridCycle).count(), 0)

        self.mock_notifier.send_message.assert_called_once()
        msg = self.mock_notifier.send_message.call_args[0][0]
        self.assertIn("NDX 行情数据不可用", msg)
        self.assertIn("UNAVAILABLE", msg)
        self.assertIn("本次 Grid 监控已跳过", msg)

        logs = self._alert_logs("NDX_GRID_DATA_UNAVAILABLE")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].message, msg)

    def test_F2_unavailable_on_empty_data(self):
        """F2. 空数据 -> UNAVAILABLE 提醒"""
        self.mock_fetcher.get_ndx_data.return_value = {}
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            res = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                        check_trading_hours=False)
        self.assertEqual(res["reason"], "NO_VALID_DATA")
        msg = self.mock_notifier.send_message.call_args[0][0]
        self.assertIn("UNAVAILABLE", msg)

    def test_E3_stale_formatter_template(self):
        """E3. stale formatter 模板"""
        msg = format_ndx_grid_data_stale("2026-09-03 14:30:00-04:00")
        self.assertIn("⚠️ NDX 行情数据过期", msg)
        self.assertIn("数据状态：🔴 STALE", msg)
        self.assertIn("最后有效数据：2026-09-03 14:30:00-04:00", msg)
        self.assertIn("系统不会基于过期行情触发新的信号", msg)

    def test_F3_unavailable_formatter_template(self):
        """F3. unavailable formatter 模板"""
        msg = format_ndx_grid_data_unavailable()
        self.assertIn("⚠️ NDX 行情数据不可用", msg)
        self.assertIn("数据状态：🔴 UNAVAILABLE", msg)
        self.assertIn("系统不会在数据异常时执行信号判断", msg)


# ---------------------------------------------------------------------------
# G. AlertLog 一致性
# ---------------------------------------------------------------------------
class TestAlertLogConsistency(NotificationTestBase):
    def test_G_alertlog_message_equals_sent_message(self):
        """G. 多事件: AlertLog.message 与企业微信实际发送内容完全一致"""
        # 1) Entry
        self.mock_fetcher.get_ndx_data.return_value = make_entry_ndx_data()
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)

        entry_sent = self.mock_notifier.send_message.call_args[0][0]
        entry_log = self._alert_logs("NDX_GRID_ENTRY")[0]
        self.assertEqual(entry_log.message, entry_sent)

        # 2) STOPPED
        waiting = self.db.query(GridCycle).filter(GridCycle.status == "WAITING").first()
        start_grid_cycle(
            self.db, waiting.id,
            actual_base_price=VALID_ACTUAL["actual_base_price"],
            actual_upper_price=VALID_ACTUAL["actual_upper_price"],
            actual_lower_price=VALID_ACTUAL["actual_lower_price"],
            actual_grid_count=VALID_ACTUAL["actual_grid_count"],
            actual_leverage=VALID_ACTUAL["actual_leverage"],
        )
        self.mock_notifier.send_message.reset_mock()
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(price=23000.0)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=self.mock_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)
        stopped_sent = self.mock_notifier.send_message.call_args[0][0]
        stopped_log = self._alert_logs("NDX_GRID_STOPPED")[0]
        self.assertEqual(stopped_log.message, stopped_sent)
        self.assertTrue(stopped_log.sent_successfully)

    def test_G2_send_failure_records_full_message_with_success_false(self):
        """G2. 发送失败: sent_successfully=False, message 仍为完整 formatted message"""
        failing_notifier = MagicMock(spec=WeChatNotifier)
        failing_notifier.send_message.return_value = False

        self.mock_fetcher.get_ndx_data.return_value = make_entry_ndx_data()
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=failing_notifier):
            res = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                        check_trading_hours=False)
        self.assertEqual(res["status"], "CREATED_WAITING")

        log = self._alert_logs("NDX_GRID_ENTRY")[0]
        self.assertFalse(log.sent_successfully)
        self.assertEqual(log.error_message, "Failed to send WeChat notification")
        # message 是完整格式化文本 (非空、多行、含关键字段)
        self.assertIn("NDX Grid 入场信号", log.message)
        self.assertIn("Base：28,920.50", log.message)
        self.assertGreater(log.message.count("\n"), 10)

    def test_G3_send_exception_records_full_message(self):
        """G3. 发送抛异常: 状态不受影响, AlertLog 记录完整 message + success=False"""
        failing_notifier = MagicMock(spec=WeChatNotifier)
        failing_notifier.send_message.side_effect = ConnectionError("webhook down")

        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        start_grid_cycle(
            self.db, cycle.id,
            actual_base_price=VALID_ACTUAL["actual_base_price"],
            actual_upper_price=VALID_ACTUAL["actual_upper_price"],
            actual_lower_price=VALID_ACTUAL["actual_lower_price"],
            actual_grid_count=VALID_ACTUAL["actual_grid_count"],
            actual_leverage=VALID_ACTUAL["actual_leverage"],
        )
        self.mock_fetcher.get_ndx_data.return_value = make_ndx_data(price=34710.0)
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=failing_notifier):
            res = check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                        check_trading_hours=False)
        self.assertEqual(res["status"], "CLOSED")

        self.db.expire_all()
        closed = self.db.query(GridCycle).filter(GridCycle.id == cycle.id).first()
        self.assertEqual(closed.status, "CLOSED")

        log = self._alert_logs("NDX_GRID_CLOSED")[0]
        self.assertFalse(log.sent_successfully)
        self.assertIn("NDX Grid 已触及上限", log.message)

    def test_G4_legacy_daily_report_alertlog_format_preserved(self):
        """G4. 日报 AlertLog 保持既有 dict-json 包装格式 (兼容现有查询)"""
        from app.scheduler.jobs import _log_alert
        _log_alert(self.db, {"alert_type": "NDX_GRID_DAILY_REPORT",
                             "rule_name": "NDX Grid Daily Report",
                             "message": "完整日报文本"}, True)
        log = self._alert_logs("NDX_GRID_DAILY_REPORT")[0]
        stored = json.loads(log.message)
        self.assertEqual(stored["message"], "完整日报文本")


# ---------------------------------------------------------------------------
# H. Secret redaction
# ---------------------------------------------------------------------------
class TestSecretRedactionUnified(NotificationTestBase):
    WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TOPSECRET123"

    def _all_messages(self):
        indicators = dict(ENTRY_INDICATORS)
        cycle = dict(SUGGESTED)
        cycle.update({"close_reason": "LOWER_BREACHED"})
        cycle_closed = dict(SUGGESTED)
        cycle_closed.update({"close_reason": "UPPER_REACHED"})
        actual_cycle = dict(VALID_ACTUAL)
        actual_cycle.update({"close_reason": "MANUAL_CLOSE"})
        report = {
            "date": "2026-09-04",
            "dashboard": {"ndx": {"last_price": 28920.50, "rsi": 34.2, "ma200": 27050.30,
                                  "price_1y_ago": 27000.0, "entry_signal": True,
                                  "is_data_valid": True, "is_data_fresh": True},
                          "running_cycle": None, "waiting_cycle": None,
                          "theoretical_grid_position": None},
            "latest_cycle": None,
            "strategy": {"upper_pct": 0.20, "lower_pct": 0.20, "grid_count": 200,
                         "leverage": 5.0, "rsi_threshold": 35.0},
            "breadth": None,
        }
        return [
            format_ndx_grid_entry(cycle, 28920.50, indicators=indicators, rsi_threshold=35.0),
            format_ndx_grid_proximity(29180.20, 37.2, 35.0, indicators=indicators),
            format_ndx_grid_stopped(cycle, 23080.40, indicators=indicators),
            format_ndx_grid_stop_loss(cycle, 20800.40, indicators=indicators),
            format_ndx_grid_closed(cycle_closed, 34760.30, indicators=indicators),
            format_ndx_grid_manual_close(actual_cycle, 28920.50, indicators=indicators),
            format_ndx_grid_data_stale("2026-09-03"),
            format_ndx_grid_data_unavailable(),
            format_ndx_grid_daily_report(report),
        ]

    def test_H_no_secrets_in_any_message(self):
        """H. webhook secret 不出现在任何通知消息中"""
        for msg in self._all_messages():
            self.assertNotIn("TOPSECRET123", msg)
            self.assertNotIn("key=", msg)
            self.assertNotIn("webhook", msg)

    def test_H2_redact_secrets_in_error_log(self):
        """H2. 异常日志中的 webhook key 仍被脱敏"""
        leaky = "HTTPSConnectionPool url=/cgi-bin/webhook/send?key=TOPSECRET123"
        redacted = _redact_secrets(leaky)
        self.assertNotIn("TOPSECRET123", redacted)
        self.assertIn("key=***", redacted)

    def test_H3_failure_log_does_not_leak_webhook(self):
        """H3. 发送失败路径: AlertLog / error_message 不包含 webhook secret"""
        failing_notifier = MagicMock(spec=WeChatNotifier)
        failing_notifier.send_message.return_value = False

        self.mock_fetcher.get_ndx_data.return_value = make_entry_ndx_data()
        self.mock_config.get_wechat_webhook_url.return_value = self.WEBHOOK
        with patch("app.scheduler.jobs.get_wechat_notifier", return_value=failing_notifier):
            check_ndx_grid_cycles(self.mock_fetcher, db=self.db, config=self.mock_config,
                                  check_trading_hours=False)

        log = self._alert_logs("NDX_GRID_ENTRY")[0]
        self.assertNotIn("TOPSECRET123", log.message)
        self.assertNotIn("TOPSECRET123", log.error_message or "")


# ---------------------------------------------------------------------------
# I. 手动关闭通知 (状态变化)
# ---------------------------------------------------------------------------
class TestManualCloseNotification(NotificationTestBase):
    def setUp(self):
        super().setUp()
        from app.main import app
        from app.database.init_db import get_db
        app.dependency_overrides[get_db] = lambda: self.db
        self.app = app
        self.client = TestClient(app, follow_redirects=False)
        self.client.cookies.set("admin_logged_in", "true")

    def tearDown(self):
        self.client.close()
        self.app.dependency_overrides.clear()
        super().tearDown()

    def test_I_manual_close_sends_notification(self):
        """I. RUNNING 手动关闭 -> 🔄 状态更新通知 + AlertLog 一致 + 不虚构自动操作"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        start_grid_cycle(
            self.db, cycle.id,
            actual_base_price=VALID_ACTUAL["actual_base_price"],
            actual_upper_price=VALID_ACTUAL["actual_upper_price"],
            actual_lower_price=VALID_ACTUAL["actual_lower_price"],
            actual_grid_count=VALID_ACTUAL["actual_grid_count"],
            actual_leverage=VALID_ACTUAL["actual_leverage"],
        )

        with patch("app.api.grid_api._get_wechat_notifier", return_value=self.mock_notifier):
            res = self.client.post(f"/api/grid/{cycle.id}/close")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "CLOSED")
        self.assertEqual(res.json()["close_reason"], "MANUAL_CLOSE")

        self.mock_notifier.send_message.assert_called_once()
        msg = self.mock_notifier.send_message.call_args[0][0]
        self.assertIn("🔄 NDX Grid 状态更新", msg)
        self.assertIn("已手动关闭", msg)
        self.assertIn("Base：28,920.50", msg)
        self.assertIn("本轮 Grid 已结束", msg)
        self.assertIn("等待下一次入场信号", msg)
        self.assertNotIn("已平仓", msg)

        logs = self._alert_logs("NDX_GRID_MANUAL_CLOSE")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].message, msg)

    def test_I2_manual_close_without_webhook_no_notification(self):
        """I2. 未配置 webhook -> 手动关闭正常完成, 不发送通知"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        start_grid_cycle(
            self.db, cycle.id,
            actual_base_price=VALID_ACTUAL["actual_base_price"],
            actual_upper_price=VALID_ACTUAL["actual_upper_price"],
            actual_lower_price=VALID_ACTUAL["actual_lower_price"],
            actual_grid_count=VALID_ACTUAL["actual_grid_count"],
            actual_leverage=VALID_ACTUAL["actual_leverage"],
        )
        with patch("app.api.grid_api._get_wechat_notifier", return_value=None):
            result = grid_service.close_running_cycle(self.db, cycle.id)
        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(len(self._alert_logs("NDX_GRID_MANUAL_CLOSE")), 0)


# ---------------------------------------------------------------------------
# 日报 formatter 补充 (正式模板; dedup/一致性由 phase6 测试覆盖)
# ---------------------------------------------------------------------------
class TestDailyReportFormatter(NotificationTestBase):
    def _report(self, **ndx_overrides):
        ndx = {
            "last_price": 29545.79, "rsi": 53.60, "ma200": 27029.50,
            "price_1y_ago": 23630.95, "entry_signal": False,
            "is_data_valid": True, "is_data_fresh": True,
            "prev_close": 29305.0,
        }
        ndx.update(ndx_overrides)
        return {
            "date": "2026-09-04",
            "dashboard": {
                "ndx": ndx,
                "running_cycle": None, "waiting_cycle": None,
                "theoretical_grid_position": None,
            },
            "latest_cycle": None,
            "strategy": {"upper_pct": 0.20, "lower_pct": 0.20, "grid_count": 200,
                         "leverage": 5.0, "rsi_threshold": 35.0},
            "breadth": {"sp500": {"price": 6500.0, "prev_close": 6456.6, "change_pct": 0.67},
                        "vix": {"price": 14.32, "prev_close": 15.20, "change_pct": -5.79}},
        }

    def test_I3_daily_report_final_template(self):
        """I3. 日报最终模板: 市场简报 + 大盘 + 入场信号 + 简洁 Grid 状态 + 规则化市场状态"""
        msg = format_ndx_grid_daily_report(self._report())
        self.assertIn("📊 NDX 每日市场简报", msg)
        self.assertIn("价格：29,545.79", msg)
        self.assertIn("+0.82%", msg)
        self.assertIn("数据状态：🟢 正常", msg)
        self.assertIn("标普500：6,500.00", msg)
        self.assertIn("VIX：14.32", msg)
        self.assertIn("状态：⚪ 未触发", msg)
        self.assertIn("入场阈值：35.00", msg)
        self.assertIn("距离阈值：18.60", msg)
        self.assertIn("当前：⚪ 无运行中的 Grid", msg)
        self.assertIn("默认区间：±20%", msg)
        self.assertIn("网格：200格", msg)
        self.assertIn("杠杆：5.0x", msg)
        # 市场状态: 规则生成的事实描述
        self.assertIn("NDX 当前位于 SMA200 上方 9.31%", msg)
        self.assertIn("过去一年上涨 25.03%", msg)
        self.assertIn("RSI 尚未进入入场区域, 距离入场阈值还有 18.60", msg)
        # 日报不展开固定 Grid 参数明细
        self.assertNotIn("Base：", msg)
        self.assertNotIn("每格：", msg)
        # VIX 下跌显示 🟢 (风险回落)
        self.assertIn("🟢 -5.79%", msg)

    def test_I4_daily_report_missing_breadth_renders_na(self):
        """I4. S&P500/VIX 缺失 -> N/A, 不影响日报与 NDX 主数据"""
        report = self._report()
        report["breadth"] = None
        msg = format_ndx_grid_daily_report(report)
        self.assertIn("标普500：N/A", msg)
        self.assertIn("VIX：N/A", msg)
        self.assertIn("价格：29,545.79", msg)

    def test_I5_daily_report_rsi_near_threshold(self):
        """I5. RSI 进入入场区域 -> 市场状态如实描述已触发"""
        msg = format_ndx_grid_daily_report(self._report(rsi=33.0, entry_signal=True))
        self.assertIn("状态：🟢 已触发", msg)
        self.assertIn("RSI 已进入入场区域", msg)


if __name__ == "__main__":
    unittest.main()
