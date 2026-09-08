"""
NDX Grid Stop Loss Alert Tests.

Covers:
- Config: DEFAULT_GRID_STOP_LOSS_AFTER_LOWER_PCT (default 0.10, env/db override, invalid fallback)
- STOPPED boundary: price > Lower -> RUNNING; price == Lower -> STOPPED; price < Lower -> STOPPED
- STOP_LOSS trigger: price <= Lower × (1 - pct) -> sends NDX_GRID_STOP_LOSS once
- Cycle-level dedup: 22,950 triggers once; 22,900 / 22,000 never repeat
- No trigger before alert price (23,000 -> STOPPED, no STOP_LOSS)
- Rebound after STOPPED: never triggers STOP_LOSS, cycle stays STOPPED (no auto-restore)
- After STOP_LOSS: price recovers -> still STOPPED, no auto-restore / no auto rebuild
- New cycle: new Lower / new dedup key -> STOP_LOSS can fire again
- Runtime pct: stop price = Lower × (1 - pct), pct from config (no magic 0.90)
- Formatters: STOP_LOSS template + AlertLog message equality
"""
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from pytz import timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, GridCycle, AlertLog
from app.alerts.grid_cycle import (
    create_waiting_grid_cycle,
    start_grid_cycle,
    stop_grid_cycle,
)
from app.alerts import dedup
from app.alerts.grid_monitor import process_ndx_grid_cycle, _resolve_stop_loss_after_lower_pct
from app.config import Config
from app.notification.wechat import WeChatNotifier, format_ndx_grid_stop_loss

et_tz = timezone("America/New_York")

# 网格: Base 30000 / ±15% -> Upper 34500 / Lower 25500 (止损提醒线 25500 × 0.90 = 22950)
SUGGESTED = {
    "base_price": 30000.0,
    "upper_price": 34500.0,
    "lower_price": 25500.0,
    "grid_count": 300,
    "leverage": 3.0,
}

VALID_ACTUAL = {
    "actual_base_price": 30000.0,
    "actual_upper_price": 34500.0,
    "actual_lower_price": 25500.0,
    "actual_grid_count": 300,
    "actual_leverage": 3.0,
    "actual_margin": 1000.0,
}

LOWER = 25500.0
STOP_LOSS_ALERT_PRICE = 22950.0  # 25500 × (1 - 0.10)

_UNSET = object()  # 区分 "未传 notifier" 与 "显式传 None"


def make_ndx_data(price, rsi=50.0):
    """默认数据不满足 Entry 三条件 (RSI=50 且 price < 1y ago), 避免测试受 Entry 信号干扰"""
    return {
        "ticker": "^NDX",
        "is_data_valid": True,
        "last_price": price,
        "rsi": rsi,
        "ma200": 26000.0,
        "is_above_sma200_3d": False,
        "price_1y_ago": 24000.0,
        "prev_close": 26000.0,
        "data_timestamp": str(datetime.now(et_tz)),
    }


class StopLossTestBase(unittest.TestCase):
    def setUp(self):
        dedup.clear_dedup()
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

        self.mock_notifier = MagicMock(spec=WeChatNotifier)
        self.mock_notifier.send_message.return_value = True

        self.mock_config = MagicMock()
        self.mock_config.get_wechat_webhook_url.return_value = "https://mock.webhook/key=SECRET"
        self.mock_config.get_rsi_threshold.return_value = 35.0
        self.mock_config.get_default_grid_upper_pct.return_value = 0.15
        self.mock_config.get_default_grid_lower_pct.return_value = 0.15
        self.mock_config.get_default_grid_count.return_value = 300
        self.mock_config.get_default_grid_leverage.return_value = 3.0
        self.mock_config.get_default_grid_stop_loss_after_lower_pct.return_value = 0.10

    def tearDown(self):
        dedup.clear_dedup()
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _make_running(self):
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        start_grid_cycle(self.db, cycle.id, **dict(VALID_ACTUAL))
        return cycle

    def _run_monitor(self, price, notifier=_UNSET, config=None):
        """模拟一轮 5 分钟监控 (相当于 scheduler 每 5 分钟调用一次)"""
        return process_ndx_grid_cycle(
            self.db,
            make_ndx_data(price),
            notifier=self.mock_notifier if notifier is _UNSET else notifier,
            config=config if config is not None else self.mock_config,
        )

    def _sent_messages(self):
        return [c.args[0] for c in self.mock_notifier.send_message.call_args_list]

    def _alert_logs(self, alert_type):
        return self.db.query(AlertLog).filter(AlertLog.alert_type == alert_type).all()


class TestStopLossConfig(unittest.TestCase):
    def test_default_pct(self):
        """配置默认 0.10 (db/env 均缺失)"""
        self.assertEqual(Config({}).get_default_grid_stop_loss_after_lower_pct(), 0.10)

    def test_env_override(self):
        """环境变量覆盖"""
        with patch.dict("os.environ", {"DEFAULT_GRID_STOP_LOSS_AFTER_LOWER_PCT": "0.05"}):
            self.assertEqual(Config({}).get_default_grid_stop_loss_after_lower_pct(), 0.05)

    def test_db_override(self):
        """DB dict 配置优先于 env"""
        with patch.dict("os.environ", {"DEFAULT_GRID_STOP_LOSS_AFTER_LOWER_PCT": "0.05"}):
            cfg = Config({"default_grid_stop_loss_after_lower_pct": "0.15"})
            self.assertEqual(cfg.get_default_grid_stop_loss_after_lower_pct(), 0.15)

    def test_monitor_resolves_valid_and_invalid(self):
        """monitor 解析: 合法值生效; 非法/缺失/越界回退 0.10"""
        self.assertEqual(_resolve_stop_loss_after_lower_pct(Config({
            "default_grid_stop_loss_after_lower_pct": 0.05
        })), 0.05)
        self.assertEqual(_resolve_stop_loss_after_lower_pct(Config({
            "default_grid_stop_loss_after_lower_pct": "bogus"
        })), 0.10)
        self.assertEqual(_resolve_stop_loss_after_lower_pct(Config({})), 0.10)
        self.assertEqual(_resolve_stop_loss_after_lower_pct(None), 0.10)
        for bad in (0, -0.1, 1.0, 1.5, True):
            self.assertEqual(_resolve_stop_loss_after_lower_pct(Config({
                "default_grid_stop_loss_after_lower_pct": bad
            })), 0.10)


class TestStoppedBoundary(StopLossTestBase):
    def test_price_above_lower_running(self):
        """price > Lower -> 不 STOPPED (RUNNING_NO_CHANGE)"""
        self._make_running()
        res = self._run_monitor(25500.01)
        self.assertEqual(res["status"], "RUNNING_NO_CHANGE")

    def test_price_equal_lower_stopped(self):
        """price == Lower -> STOPPED (下轨触发含等于)"""
        self._make_running()
        res = self._run_monitor(LOWER)
        self.assertEqual(res["status"], "STOPPED")
        self.assertEqual(res["reason"], "LOWER_BREACHED")

    def test_price_below_lower_stopped(self):
        """price < Lower -> STOPPED"""
        self._make_running()
        res = self._run_monitor(25499.99)
        self.assertEqual(res["status"], "STOPPED")


class TestStopLossTrigger(StopLossTestBase):
    def test_below_alert_price_no_stop_loss(self):
        """23,000: STOPPED 后处于观察区, 但未达 22,950 -> 不发送 STOP_LOSS"""
        self._make_running()
        res = self._run_monitor(LOWER)          # STOPPED
        self.assertEqual(res["status"], "STOPPED")
        self.mock_notifier.send_message.reset_mock()

        res = self._run_monitor(23000.0)
        self.assertEqual(res["status"], "NO_SIGNAL")
        self.assertEqual(res["stop_loss"]["triggered"], False)
        self.assertEqual(len(self._alert_logs("NDX_GRID_STOP_LOSS")), 0)
        self.assertEqual(len(self._sent_messages()), 0)

    def test_at_alert_price_triggers_once(self):
        """22,950 (== Lower × 0.90) -> 发送一次 STOP_LOSS; AlertLog 保存完整消息"""
        self._make_running()
        self._run_monitor(LOWER)
        self.mock_notifier.send_message.reset_mock()

        res = self._run_monitor(STOP_LOSS_ALERT_PRICE)
        self.assertEqual(res["status"], "STOP_LOSS_ALERTED")
        self.assertEqual(res["notified"], True)
        self.assertEqual(res["stop_loss_alert_price"], STOP_LOSS_ALERT_PRICE)

        messages = self._sent_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("🛑 NDX Grid 止损提醒", messages[0])
        self.assertIn("止损提醒线：22,950.00", messages[0])

        logs = self._alert_logs("NDX_GRID_STOP_LOSS")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].message, messages[0])
        self.assertTrue(logs[0].sent_successfully)

    def test_no_repeat_below_alert_price(self):
        """22,900 / 22,500 / 21,000: 不再重复发送 (cycle 级 dedup)"""
        self._make_running()
        self._run_monitor(LOWER)
        self._run_monitor(STOP_LOSS_ALERT_PRICE)   # 触发一次
        self.mock_notifier.send_message.reset_mock()

        for price in (22900.0, 22500.0, 21000.0):
            res = self._run_monitor(price)
            self.assertEqual(res["status"], "NO_SIGNAL")
            self.assertEqual(res["stop_loss"]["deduplicated"], True)
        self.assertEqual(len(self._sent_messages()), 0)
        self.assertEqual(len(self._alert_logs("NDX_GRID_STOP_LOSS")), 1)

    def test_stop_loss_does_not_change_cycle_status(self):
        """STOP_LOSS 触发后 GridCycle 仍为 STOPPED (不新增状态 / 不自动交易)"""
        self._make_running()
        self._run_monitor(LOWER)
        self._run_monitor(STOP_LOSS_ALERT_PRICE)

        stopped = self.db.query(GridCycle).filter(GridCycle.status == "STOPPED").all()
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0].close_reason, "LOWER_BREACHED")
        self.assertEqual(
            self.db.query(GridCycle).filter(GridCycle.status.in_(["RUNNING", "WAITING"])).count(), 0
        )

    def test_rebound_after_stopped_no_stop_loss(self):
        """跌破 Lower 后反弹: 不触发 STOP_LOSS; Grid 保持 STOPPED (不自动恢复 RUNNING)"""
        self._make_running()
        self._run_monitor(LOWER)          # STOPPED
        self.mock_notifier.send_message.reset_mock()

        for price in (24000.0, 25000.0, 25700.0, 26000.0, 27000.0):
            res = self._run_monitor(price)
            self.assertEqual(res["status"], "NO_SIGNAL")
            self.assertEqual(res["stop_loss"]["triggered"], False)
        self.assertEqual(len(self._sent_messages()), 0)

        self.assertEqual(
            self.db.query(GridCycle).filter(GridCycle.status == "STOPPED").count(), 1
        )
        self.assertEqual(
            self.db.query(GridCycle).filter(GridCycle.status == "RUNNING").count(), 0
        )

    def test_rebound_then_crash_triggers(self):
        """反弹后 (从未触发过) 再次跌破止损提醒线 -> 正常触发"""
        self._make_running()
        self._run_monitor(LOWER)
        self.mock_notifier.send_message.reset_mock()
        self._run_monitor(26000.0)        # 反弹
        self._run_monitor(22000.0)        # 跌破止损线 -> 触发一次
        self.assertEqual(len(self._sent_messages()), 1)
        self.assertIn("🛑 NDX Grid 止损提醒", self._sent_messages()[0])

    def test_after_stop_loss_no_auto_restore(self):
        """STOP_LOSS 后价格回升到 25,000 -> 仍 STOPPED, 不自动恢复 / 不自动重建"""
        self._make_running()
        self._run_monitor(LOWER)
        self._run_monitor(STOP_LOSS_ALERT_PRICE)
        self.mock_notifier.send_message.reset_mock()

        res = self._run_monitor(25000.0)
        self.assertEqual(res["status"], "NO_SIGNAL")
        self.assertEqual(res["stop_loss"]["triggered"], False)
        self.assertEqual(
            self.db.query(GridCycle).filter(GridCycle.status == "STOPPED").count(), 1
        )
        self.assertEqual(
            self.db.query(GridCycle).filter(GridCycle.status.in_(["WAITING", "RUNNING"])).count(), 0
        )
        self.assertEqual(len(self._sent_messages()), 0)

    def test_stale_data_skips_stop_loss(self):
        """数据过期 (fail-closed): 不触发 STOP_LOSS"""
        self._make_running()
        self._run_monitor(LOWER)
        self.mock_notifier.send_message.reset_mock()

        stale = make_ndx_data(22000.0)
        stale["data_timestamp"] = str(datetime.now(et_tz).replace(year=2020))
        res = process_ndx_grid_cycle(self.db, stale, notifier=self.mock_notifier,
                                     config=self.mock_config)
        self.assertEqual(res["status"], "SKIPPED")
        self.assertEqual(len(self._alert_logs("NDX_GRID_STOP_LOSS")), 0)


class TestStopLossNewCycle(StopLossTestBase):
    def _close_old_and_create_new_entry(self):
        """旧 cycle STOP_LOSS 已发送 -> 模拟新的 Entry signal 创建新 cycle 并启动"""
        self._make_running()
        self._run_monitor(LOWER)
        self._run_monitor(STOP_LOSS_ALERT_PRICE)   # 旧 cycle STOP_LOSS 已发送
        self.assertEqual(len(self._sent_messages()), 2)  # STOPPED + STOP_LOSS
        self.mock_notifier.send_message.reset_mock()

        # 新 Entry (价格回到入场区域, 生成新的 Lower)
        entry_data = make_ndx_data(23000.0, rsi=30.0)
        entry_data["is_above_sma200_3d"] = True
        entry_data["price_1y_ago"] = 22000.0
        res = process_ndx_grid_cycle(self.db, entry_data, notifier=self.mock_notifier,
                                     config=self.mock_config)
        self.assertEqual(res["status"], "CREATED_WAITING")
        self.mock_notifier.send_message.reset_mock()  # 清掉 Entry 通知, 只关注止损通知

        new_cycle = self.db.query(GridCycle).filter(GridCycle.status == "WAITING").first()
        new_actual = dict(VALID_ACTUAL)
        new_actual.update({
            "actual_base_price": 23000.0,
            "actual_upper_price": 26450.0,
            "actual_lower_price": 19550.0,
        })
        start_grid_cycle(self.db, new_cycle.id, **new_actual)
        return new_cycle

    def test_new_cycle_can_trigger_stop_loss_again(self):
        """新 cycle: 新 Lower (19,550) -> 新止损线 (17,595) -> 可再次发送 STOP_LOSS"""
        self._close_old_and_create_new_entry()

        # 第一轮: 新 cycle 跌破新 Lower -> STOPPED
        res = self._run_monitor(17595.0)
        self.assertEqual(res["status"], "STOPPED")
        self.mock_notifier.send_message.reset_mock()
        # 第二轮: 价格仍 <= 新止损线 (17595 = 19550 × 0.90) -> 触发新 cycle 的 STOP_LOSS
        res = self._run_monitor(17595.0)
        self.assertEqual(res["status"], "STOP_LOSS_ALERTED")
        self.assertEqual(res["stop_loss_alert_price"], 17595.0)
        messages = self._sent_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("🛑 NDX Grid 止损提醒", messages[0])
        self.assertIn("止损提醒线：17,595.00", messages[0])

        # 新 cycle 内同样只发送一次
        self.mock_notifier.send_message.reset_mock()
        res = self._run_monitor(17000.0)
        self.assertEqual(res["status"], "NO_SIGNAL")
        self.assertEqual(res["stop_loss"]["deduplicated"], True)
        self.assertEqual(len(self._sent_messages()), 0)

        # 两个 STOPPED cycle, 各自一次 STOP_LOSS 记录
        self.assertEqual(len(self._alert_logs("NDX_GRID_STOP_LOSS")), 2)

    def test_old_cycle_does_not_interfere_with_new_cycle(self):
        """新 cycle RUNNING 期间: 价格未达新止损线 -> 不触发 (新旧 cycle 互不干扰)"""
        self._close_old_and_create_new_entry()
        res = self._run_monitor(19540.0)   # < 新 Lower -> STOPPED (新 cycle)
        self.assertEqual(res["status"], "STOPPED")
        res = self._run_monitor(18000.0)   # > 新止损线 17595 -> 仅观察
        self.assertEqual(res["stop_loss"]["triggered"], False)
        self.assertEqual(len(self._alert_logs("NDX_GRID_STOP_LOSS")), 1)  # 仅旧 cycle 那条


class TestStopLossDedupSemantics(StopLossTestBase):
    def test_cycle_key_and_dedup(self):
        """dedup 语义: 同一 key 只 True 一次; 不同 cycle key 互不影响"""
        from app.alerts.dedup import stop_loss_cycle_key, should_alert_stop_loss, is_stop_loss_alerted

        k1 = stop_loss_cycle_key(1)
        k2 = stop_loss_cycle_key(2)
        self.assertTrue(should_alert_stop_loss(k1))
        self.assertFalse(should_alert_stop_loss(k1))
        self.assertTrue(is_stop_loss_alerted(k1))
        self.assertFalse(is_stop_loss_alerted(k2))
        self.assertTrue(should_alert_stop_loss(k2))    # 新 cycle 仍可发送
        self.assertTrue(is_stop_loss_alerted(k2))

    def test_no_notifier_does_not_consume_dedup(self):
        """未配置 webhook: 条件满足但不发送, 也不占用去重名额 (配置后仍可提醒)"""
        self._make_running()
        self._run_monitor(LOWER)

        # notifier=None: 未配置 webhook, 不发送
        res = self._run_monitor(STOP_LOSS_ALERT_PRICE, notifier=None)
        self.assertEqual(res["status"], "STOP_LOSS_ALERTED")
        self.assertEqual(res["notified"], False)
        self.assertEqual(len(self._alert_logs("NDX_GRID_STOP_LOSS")), 0)

        # 配置 notifier 后 (价格继续下跌) 仍可正常发送一次
        res = self._run_monitor(22500.0)
        self.assertEqual(res["status"], "STOP_LOSS_ALERTED")
        self.assertEqual(res["notified"], True)
        self.assertEqual(len(self._alert_logs("NDX_GRID_STOP_LOSS")), 1)


class TestNoStoppedCycle(StopLossTestBase):
    def test_no_cycles_no_stop_loss(self):
        """无任何 cycle -> 不评估 STOP_LOSS (不影响 Entry 流程)"""
        res = self._run_monitor(28000.0)
        self.assertEqual(res["status"], "NO_SIGNAL")
        self.assertNotIn("stop_loss", res)
        self.assertEqual(len(self._alert_logs("NDX_GRID_STOP_LOSS")), 0)

    def test_stopped_cycle_without_actual_lower_skipped(self):
        """STOPPED cycle 无 actual 参数 (异常数据) -> 不评估止损 (不虚构)"""
        cycle = create_waiting_grid_cycle(self.db, dict(SUGGESTED))
        cycle.status = "STOPPED"  # 直接改库模拟异常数据 (正常流程 STOPPED 必有 actual 参数)
        cycle.close_reason = "LOWER_BREACHED"
        self.db.commit()

        res = self._run_monitor(20000.0)
        self.assertEqual(res["status"], "NO_SIGNAL")
        self.assertEqual(res["stop_loss"]["evaluated"], False)
        self.assertEqual(res["stop_loss"]["reason"], "NO_ACTUAL_LOWER")

    def test_custom_pct_from_config(self):
        """配置 pct=0.05 -> 止损线 = 25500 × 0.95 = 24225 (运行时可调, 非硬编码 0.90)"""
        cfg = Config({"default_grid_stop_loss_after_lower_pct": 0.05})
        self._make_running()
        self._run_monitor(LOWER)
        self.mock_notifier.send_message.reset_mock()

        # 24,300 在 0.05 口径下仍属观察区 (止损线 24,225)
        res = self._run_monitor(24300.0, config=cfg)
        self.assertEqual(res["status"], "NO_SIGNAL")
        self.assertEqual(res["stop_loss"]["triggered"], False)
        self.assertEqual(res["stop_loss"]["stop_loss_alert_price"], 24225.0)
        # 24,225 == 止损线 -> 触发
        res = self._run_monitor(24225.0, config=cfg)
        self.assertEqual(res["status"], "STOP_LOSS_ALERTED")
        self.assertEqual(res["stop_loss_alert_price"], 24225.0)
        self.assertIn("并进一步下跌 5%", self._sent_messages()[0])


class TestStopLossFormatter(unittest.TestCase):
    def test_stop_loss_template(self):
        """STOP_LOSS 模板: 止损提醒线 = Lower × 0.90, STOPPED 语义, 手动处理提示"""
        cycle = dict(VALID_ACTUAL)
        cycle["close_reason"] = "LOWER_BREACHED"
        indicators = {"rsi": 24.60, "ma200": 26980.20, "prev_close": 23872.72}
        msg = format_ndx_grid_stop_loss(cycle, 22900.40, indicators=indicators,
                                        stop_loss_after_lower_pct=0.10)
        self.assertIn("🛑 NDX Grid 止损提醒", msg)
        self.assertIn("并进一步下跌 10%", msg)
        self.assertIn("当前价格：22,900.40", msg)
        self.assertIn("RSI(14)：24.60", msg)
        self.assertIn("Grid Lower：25,500.00", msg)
        self.assertIn("止损提醒线：22,950.00", msg)
        self.assertIn("状态：🔴 已触发", msg)
        self.assertIn("Base：30,000.00", msg)
        self.assertIn("Upper：34,500.00", msg)
        self.assertIn("网格：300格", msg)
        self.assertIn("杠杆：3.0x", msg)
        self.assertIn("状态：STOPPED", msg)
        self.assertIn("原因：价格跌破 Lower 后继续下跌 10%", msg)
        self.assertIn("请检查交易所实际 Grid 及持仓情况", msg)
        self.assertIn("系统不会自动平仓", msg)
        self.assertIn("不会自动重新建立 Grid", msg)
        self.assertIn("不连接交易所，不自动交易", msg)
        # 不声称已自动平仓
        self.assertNotIn("已自动平仓", msg)

    def test_stop_loss_orm_cycle(self):
        """STOP_LOSS formatter 接受 GridCycle ORM 对象 (actual_*)"""
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        Base.metadata.create_all(bind=engine)
        S = sessionmaker(bind=engine)
        s = S()
        try:
            cycle = create_waiting_grid_cycle(s, dict(SUGGESTED))
            start_grid_cycle(s, cycle.id, **dict(VALID_ACTUAL))
            stop_grid_cycle(s, cycle.id, reason="LOWER_BREACHED")
            s.refresh(cycle)
            msg = format_ndx_grid_stop_loss(cycle, 22900.40, stop_loss_after_lower_pct=0.10)
            self.assertIn("止损提醒线：22,950.00", msg)
            self.assertIn("状态：STOPPED", msg)
        finally:
            s.close()
            Base.metadata.drop_all(bind=engine)

    def test_stop_loss_invalid_pct_fallback(self):
        """非法 pct -> 回退默认 0.10"""
        cycle = dict(VALID_ACTUAL)
        msg = format_ndx_grid_stop_loss(cycle, 22900.40, stop_loss_after_lower_pct="bogus")
        self.assertIn("止损提醒线：22,950.00", msg)


if __name__ == "__main__":
    unittest.main()
